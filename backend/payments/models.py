import uuid

from django.db import models

from .enums import FailureCode, LedgerEventType, PaymentStatus, WebhookOutcome


class LedgerImmutableError(Exception):
    """Raised by the ORM guards when code tries to mutate or delete ledger rows.

    The real enforcement is the Postgres trigger in migration 0002; these
    guards exist so a mistake fails with a readable message before it hits
    the database.
    """


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.PositiveIntegerField(help_text="Integer minor units (cents).")
    currency = models.CharField(max_length=3, default="USD")
    payment_token = models.CharField(max_length=64)
    method = models.CharField(max_length=8, blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    brand_or_bank_type = models.CharField(max_length=32, blank=True)
    # Nullable on purpose: NULL means "not submitted yet" and must not collide under UNIQUE.
    processor_reference = models.CharField(  # noqa: DJ001
        max_length=64, unique=True, null=True, blank=True
    )
    # Denormalised cache of the ledger's latest to_status. Written only inside
    # the same transaction (and row lock) as the ledger entry that changes it.
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    failure_code = models.CharField(  # noqa: DJ001 - contract says null when absent
        max_length=32, choices=FailureCode.choices, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = (
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payment_amount_positive"
            ),
        )

    def __str__(self):
        return f"{self.id} {self.status}"

    @property
    def masked_reference(self) -> str | None:
        return mask_reference(self.processor_reference)


def mask_reference(reference: str | None) -> str | None:
    if not reference:
        return reference
    prefix, _, body = reference.partition("_")
    tail = body[-4:] if body else reference[-4:]
    return f"{prefix}_…{tail}" if body else f"…{tail}"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise LedgerImmutableError("ledger entries cannot be updated")

    def delete(self):
        raise LedgerImmutableError("ledger entries cannot be deleted")

    def bulk_update(self, *args, **kwargs):
        raise LedgerImmutableError("ledger entries cannot be updated")


class LedgerEntry(models.Model):
    """One immutable row per state change. The ledger is the source of truth;
    ``Payment.status`` is a cache of the newest row's ``to_status``.
    """

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="ledger_entries")
    sequence = models.PositiveIntegerField(help_text="1-based position within the payment.")
    event_type = models.CharField(max_length=32, choices=LedgerEventType.choices)
    from_status = models.CharField(  # noqa: DJ001 - null for the very first entry
        max_length=16, choices=PaymentStatus.choices, null=True, blank=True
    )
    to_status = models.CharField(max_length=16, choices=PaymentStatus.choices)
    failure_code = models.CharField(  # noqa: DJ001 - contract says null when absent
        max_length=32, choices=FailureCode.choices, null=True, blank=True
    )
    amount_delta = models.IntegerField(
        default=0,
        help_text="Signed cents this entry moves (captures are positive; refunds would be negative).",
    )
    webhook_event = models.OneToOneField(
        "WebhookEvent", on_delete=models.PROTECT, null=True, blank=True, related_name="ledger_entry"
    )
    occurred_at = models.DateTimeField(help_text="When the processor says it happened.")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ("payment_id", "sequence")
        constraints = (
            models.UniqueConstraint(fields=["payment", "sequence"], name="ledger_sequence_unique"),
            # The database-level statement of "terminal states are final":
            # a payment can hold at most one entry that lands in a terminal status.
            models.UniqueConstraint(
                fields=["payment"],
                condition=models.Q(to_status__in=[PaymentStatus.SUCCEEDED, PaymentStatus.FAILED]),
                name="ledger_one_terminal_entry_per_payment",
            ),
        )

    def __str__(self):
        return f"{self.payment_id}#{self.sequence} {self.from_status}->{self.to_status}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise LedgerImmutableError("ledger entries cannot be modified after insert")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutableError("ledger entries cannot be deleted")


class WebhookEvent(models.Model):
    """Inbox of verified processor deliveries. ``event_id`` is unique: that
    constraint is what makes processing at-most-once, including under
    concurrent delivery (the second INSERT waits on the first's commit, then
    fails).
    """

    event_id = models.CharField(max_length=64, unique=True)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="webhook_events")
    processor_reference = models.CharField(max_length=64)
    status = models.CharField(max_length=16)
    failure_code = models.CharField(max_length=32, null=True, blank=True)  # noqa: DJ001
    occurred_at = models.DateTimeField()
    payload = models.JSONField()
    outcome = models.CharField(max_length=32, choices=WebhookOutcome.choices)
    delivery_count = models.PositiveIntegerField(default=1)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("received_at",)

    def __str__(self):
        return f"{self.event_id} {self.status} ({self.outcome})"


class IdempotencyKey(models.Model):
    key = models.CharField(max_length=255, unique=True)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="idempotency_keys"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.key
