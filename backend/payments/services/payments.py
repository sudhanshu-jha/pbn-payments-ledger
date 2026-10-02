"""Create payments idempotently and submit them to the processor."""

import hashlib
import json
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from ..enums import FailureCode, LedgerEventType, PaymentStatus
from ..gateway import ProcessorError, get_gateway
from ..ledger import append_entry, record_creation
from ..models import IdempotencyKey, Payment


logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    """Same Idempotency-Key, different request body."""


class RequestInFlightError(Exception):
    """Same key is being processed right now and has no stored response yet."""


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict
    replayed: bool


def request_hash(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_payment_idempotently(key: str, body: dict, perform) -> StoredResponse:
    """Run ``perform()`` at most once per ``key``.

    ``perform`` returns ``(status_code, response_body)``; the pair is stored
    under the key and replayed verbatim for identical retries. The UNIQUE
    constraint on ``key`` is the race guard: a concurrent duplicate's INSERT
    blocks on the index until the first request commits, then sees its row.
    """
    body_hash = request_hash(body)
    with transaction.atomic():
        row, created = IdempotencyKey.objects.get_or_create(
            key=key, defaults={"request_hash": body_hash}
        )
        if not created:
            if row.request_hash != body_hash:
                raise IdempotencyConflictError(key)
            if row.response_status is None:
                raise RequestInFlightError(key)
            return StoredResponse(row.response_status, row.response_body, replayed=True)

        status_code, response_body, payment = perform()
        row.response_status = status_code
        row.response_body = response_body
        row.payment = payment
        row.save(update_fields=["response_status", "response_body", "payment"])
        return StoredResponse(status_code, response_body, replayed=False)


CLIENT_ERRORS = frozenset({"invalid_token", "unsupported_currency", "invalid_amount"})


def submit_payment(amount: int, currency: str, payment_token: str) -> Payment:
    """Create a pending payment and ask the processor to charge the token.

    Runs in a savepoint inside the idempotency transaction:
    - a request the processor rejects outright (bad token) rolls back, so no
      payment is left behind and the 422 is what gets stored under the key;
    - a processor *outage* keeps the payment, marked failed/processor_error,
      so the client gets an honest answer and a retry with the same key
      replays it instead of charging again.
    """
    now = timezone.now()
    with transaction.atomic():
        payment = Payment.objects.create(
            amount=amount, currency=currency, payment_token=payment_token
        )
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        record_creation(payment, occurred_at=now)

        try:
            result = get_gateway().charge(payment_token, amount, currency)
        except ProcessorError as exc:
            if exc.code in CLIENT_ERRORS:
                raise  # savepoint rollback: the payment never existed
            logger.error("processor unavailable for payment %s: %s", payment.id, exc.code)
            append_entry(
                payment,
                event_type=LedgerEventType.PROCESSOR_UNAVAILABLE,
                to_status=PaymentStatus.FAILED,
                failure_code=FailureCode.PROCESSOR_ERROR,
                occurred_at=timezone.now(),
            )
            return payment

        payment.processor_reference = result.processor_reference
        payment.method = result.method
        payment.last4 = result.last4
        payment.brand_or_bank_type = result.brand_or_bank_type
        payment.save(update_fields=["processor_reference", "method", "last4", "brand_or_bank_type"])
        logger.info("payment %s submitted as %s", payment.id, payment.masked_reference)
        return payment
