import logging

from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import TokenizationError, tokenize


logger = logging.getLogger(__name__)


@method_decorator(sensitive_post_parameters(), name="dispatch")
class TokenizeView(APIView):
    """POST /processor/tokenize — the processor-hosted card/bank field.

    Raw card/bank details are accepted here and only here. They are validated,
    reduced to a token + last4, and discarded. Nothing here logs the request body.
    """

    authentication_classes = ()
    permission_classes = (AllowAny,)

    @sensitive_variables("data", "result")
    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            result = tokenize(data)
        except TokenizationError as exc:
            logger.info("tokenize rejected: %s", exc.code)
            return Response({"error": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        logger.info("tokenized %s ending %s", result.method, result.last4)
        return Response(result.as_dict(), status=status.HTTP_200_OK)
