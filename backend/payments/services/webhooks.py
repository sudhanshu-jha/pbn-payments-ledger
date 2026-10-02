"""Verify and apply processor webhooks at most once, in any order, concurrently."""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F

from ..enums import LedgerEventType, PaymentStatus, WebhookOutcome
from ..ledger import append_entry
from ..models import Payment, WebhookEvent
from ..state_machine import decide


logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Processor-Signature"
VALID_STATUSES = {s.value for s in PaymentStatus}


class InvalidSignatureError(Exception):
    pass


class MalformedEventError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class UnknownReferenceError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    event_id: str
    processor_reference: str
    status: str
    failure_code: str | None
    occurred_at: datetime
    raw: dict


@dataclass(frozen=True)
class ApplyResult:
    outcome: WebhookOutcome
    payment_id: str
    status: str


def verify_signature(raw_body: bytes, signature: str | None) -> None:
    """Constant-time HMAC-SHA256 check over the raw bytes. Fails closed."""
    secret = settings.PROCESSOR_WEBHOOK_SECRET
    if not secret or not signature:
        raise InvalidSignatureError()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise InvalidSignatureError()


def parse_event(raw_body: bytes) -> Event:
    try:
        data = json.loads(raw_body)
    except ValueError as exc:
        raise MalformedEventError("body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise MalformedEventError("body must be a JSON object")
    for field in ("event_id", "processor_reference", "status", "occurred_at"):
        if not data.get(field):
            raise MalformedEventError(f"missing field: {field}")
    if data["status"] not in VALID_STATUSES:
        raise MalformedEventError(f"unknown status: {data['status']}")
    try:
        occurred_at = datetime.fromisoformat(str(data["occurred_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedEventError("occurred_at is not an ISO-8601 timestamp") from exc
    return Event(
        event_id=str(data["event_id"]),
        processor_reference=str(data["processor_reference"]),
        status=data["status"],
        failure_code=data.get("failure_code") or None,
        occurred_at=occurred_at,
        raw=data,
    )


def apply_event(event: Event) -> ApplyResult:
    """Apply one verified event. Safe to call concurrently and repeatedly.

    Locking, in order:
      1. ``SELECT ... FOR UPDATE`` on the payment row serialises every event for
         the same payment, so decide-then-write can't interleave.
      2. INSERT into the inbox; ``UNIQUE(event_id)`` turns an identical
         concurrent/duplicate delivery into an IntegrityError on a savepoint.
      3. Only then the state machine decides and (maybe) a ledger row is written,
         all inside the same transaction as the inbox row.
    """
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .filter(processor_reference=event.processor_reference)
            .first()
        )
        if payment is None:
            raise UnknownReferenceError()

        decision = decide(payment.status, event.status)
        inbox = WebhookEvent(
            event_id=event.event_id,
            payment=payment,
            processor_reference=event.processor_reference,
            status=event.status,
            failure_code=event.failure_code,
            occurred_at=event.occurred_at,
            payload=event.raw,
            outcome=decision.outcome,
        )
        try:
            with transaction.atomic():
                inbox.save()
        except IntegrityError:
            WebhookEvent.objects.filter(event_id=event.event_id).update(
                delivery_count=F("delivery_count") + 1
            )
            logger.info("duplicate webhook %s for payment %s", event.event_id, payment.id)
            return ApplyResult(WebhookOutcome.DUPLICATE, str(payment.id), payment.status)

        if decision.applies:
            append_entry(
                payment,
                event_type=LedgerEventType.PROCESSOR_WEBHOOK,
                to_status=decision.new_status,
                failure_code=event.failure_code,
                amount_delta=payment.amount
                if decision.new_status == PaymentStatus.SUCCEEDED
                else 0,
                occurred_at=event.occurred_at,
                webhook_event=inbox,
            )
            logger.info("payment %s -> %s via %s", payment.id, decision.new_status, event.event_id)
        elif decision.outcome == WebhookOutcome.CONFLICT:
            logger.error(
                "conflicting terminal webhook %s: payment %s is %s, processor says %s",
                event.event_id,
                payment.id,
                payment.status,
                event.status,
            )
        else:
            logger.info(
                "webhook %s ignored (%s): payment %s is %s, processor says %s",
                event.event_id,
                decision.outcome,
                payment.id,
                payment.status,
                event.status,
            )
        return ApplyResult(decision.outcome, str(payment.id), payment.status)


def receive(raw_body: bytes, signature: str | None) -> ApplyResult:
    """Full receiver pipeline: verify -> parse -> apply."""
    verify_signature(raw_body, signature)
    return apply_event(parse_event(raw_body))
