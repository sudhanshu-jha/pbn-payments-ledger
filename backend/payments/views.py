import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .enums import PaymentStatus
from .gateway import ProcessorError, get_gateway
from .models import Payment
from .serializers import CreatePaymentSerializer, PaymentSerializer
from .services import payments as payment_service
from .services import webhooks as webhook_service


logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"


class PaymentListCreateView(APIView):
    """POST /api/payments (idempotent) and GET /api/payments (list, for the UI)."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request):
        queryset = Payment.objects.prefetch_related("ledger_entries").all()[:100]
        return Response(PaymentSerializer(queryset, many=True).data)

    def post(self, request):
        key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not key:
            return Response(
                {
                    "error": "missing_idempotency_key",
                    "detail": f"{IDEMPOTENCY_HEADER} header is required.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(key) > 255:
            return Response(
                {
                    "error": "invalid_idempotency_key",
                    "detail": "Key must be at most 255 characters.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        body = request.data if isinstance(request.data, dict) else {}

        def perform():
            serializer = CreatePaymentSerializer(data=body)
            if not serializer.is_valid():
                return (
                    status.HTTP_400_BAD_REQUEST,
                    {"error": "validation_error", "detail": serializer.errors},
                    None,
                )
            try:
                payment = payment_service.submit_payment(**serializer.validated_data)
            except ProcessorError as exc:
                return (
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    {"error": "invalid_payment_token" if exc.code == "invalid_token" else exc.code},
                    None,
                )
            return status.HTTP_201_CREATED, PaymentSerializer(payment).data, payment

        try:
            stored = payment_service.create_payment_idempotently(key, body, perform)
        except payment_service.IdempotencyConflictError:
            return Response(
                {
                    "error": "idempotency_key_reused",
                    "detail": "This Idempotency-Key was already used with a different request body.",
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except payment_service.RequestInFlightError:
            return Response(
                {
                    "error": "request_in_flight",
                    "detail": "A request with this key is still processing.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        response = Response(stored.body, status=stored.status_code)
        response["Idempotent-Replayed"] = "true" if stored.replayed else "false"
        return response


class PaymentDetailView(APIView):
    """GET /api/payments/{id}: current status plus full ledger history."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def get(self, request, payment_id):
        try:
            payment = Payment.objects.prefetch_related("ledger_entries").get(pk=payment_id)
        except Payment.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(PaymentSerializer(payment).data)


class PaymentReplayView(APIView):
    """POST /api/payments/{id}/replay (admin only).

    Re-requests the processor's final event for a stuck (pending) payment and
    feeds it through the exact same receiver pipeline as a live webhook:
    signature check, inbox dedup, state machine. The processor re-issues the
    event byte-for-byte (same event_id), so replaying can never double-process.
    """

    permission_classes = (IsAdminUser,)

    def post(self, request, payment_id):
        try:
            payment = Payment.objects.get(pk=payment_id)
        except Payment.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        if payment.status != PaymentStatus.PENDING:
            return Response(
                {"error": "not_pending", "detail": f"Payment is already {payment.status}."},
                status=status.HTTP_409_CONFLICT,
            )
        if not payment.processor_reference:
            return Response({"error": "never_submitted"}, status=status.HTTP_409_CONFLICT)
        try:
            signed = get_gateway().request_redelivery(payment.processor_reference)
        except ProcessorError as exc:
            return Response({"error": exc.code}, status=status.HTTP_502_BAD_GATEWAY)
        result = webhook_service.receive(signed.body, signed.signature)
        return Response(
            {"outcome": result.outcome, "payment_id": result.payment_id, "status": result.status},
            status=status.HTTP_202_ACCEPTED,
        )


@csrf_exempt
@require_POST
def processor_webhook(request):
    """POST /webhooks/processor.

    A plain Django view on purpose: the HMAC is computed over the raw request
    bytes, so nothing may parse or re-serialise the body before verification.
    """
    try:
        result = webhook_service.receive(
            request.body, request.headers.get(webhook_service.SIGNATURE_HEADER)
        )
    except webhook_service.InvalidSignatureError:
        logger.warning("webhook rejected: invalid or missing signature")
        return JsonResponse({"error": "invalid_signature"}, status=401)
    except webhook_service.MalformedEventError as exc:
        return JsonResponse({"error": "malformed_event", "detail": exc.detail}, status=400)
    except webhook_service.UnknownReferenceError:
        # 404 so a real processor keeps retrying until the charge exists on our side.
        return JsonResponse({"error": "unknown_reference"}, status=404)
    return JsonResponse(
        {"outcome": result.outcome, "payment_id": result.payment_id, "status": result.status},
        status=200,
    )


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
