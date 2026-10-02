"""Ledger helpers: derive status from entries, append entries safely."""

from django.db import transaction

from .enums import LedgerEventType, PaymentStatus
from .models import LedgerEntry, Payment


def derive_status(entries) -> tuple[str, str | None]:
    """(status, failure_code) implied purely by the ledger, ignoring Payment.status.

    Entries are read in sequence order; the newest wins. A payment with no
    entries has no status (should never happen: creation writes entry #1).
    """
    status, failure_code = None, None
    for entry in sorted(entries, key=lambda e: e.sequence):
        status, failure_code = entry.to_status, entry.failure_code
    return status, failure_code


def append_entry(
    payment: Payment,
    *,
    event_type: str,
    to_status: str,
    occurred_at,
    failure_code: str | None = None,
    amount_delta: int = 0,
    webhook_event=None,
) -> LedgerEntry:
    """Write the next ledger entry for ``payment`` and refresh its cached status.

    Caller must hold the payment row lock (``select_for_update``) inside a
    transaction; this is asserted rather than silently re-locked so the
    locking discipline stays visible at the call site.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            "append_entry must run inside a transaction holding the payment row lock"
        )
    last = payment.ledger_entries.order_by("-sequence").values_list("sequence", flat=True).first()
    entry = LedgerEntry.objects.create(
        payment=payment,
        sequence=(last or 0) + 1,
        event_type=event_type,
        from_status=payment.status if last else None,
        to_status=to_status,
        failure_code=failure_code,
        amount_delta=amount_delta,
        webhook_event=webhook_event,
        occurred_at=occurred_at,
    )
    payment.status = to_status
    payment.failure_code = failure_code
    payment.save(update_fields=["status", "failure_code", "updated_at"])
    return entry


def record_creation(payment: Payment, occurred_at) -> LedgerEntry:
    return append_entry(
        payment,
        event_type=LedgerEventType.PAYMENT_CREATED,
        to_status=PaymentStatus.PENDING,
        occurred_at=occurred_at,
        amount_delta=0,
    )
