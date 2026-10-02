from django.db import models


class PaymentStatus(models.TextChoices):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({PaymentStatus.SUCCEEDED, PaymentStatus.FAILED})


class FailureCode(models.TextChoices):
    CARD_DECLINED = "card_declined"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    PROCESSOR_ERROR = "processor_error"


class LedgerEventType(models.TextChoices):
    """Why a ledger entry was written."""

    PAYMENT_CREATED = "payment_created"  # None -> pending, charge submitted
    PROCESSOR_WEBHOOK = "processor_webhook"  # pending -> terminal via webhook
    PROCESSOR_UNAVAILABLE = "processor_unavailable"  # pending -> failed, charge call raised


class WebhookOutcome(models.TextChoices):
    """What the receiver decided to do with a verified delivery."""

    APPLIED = "applied"  # produced a ledger entry
    DUPLICATE = "duplicate"  # same event_id seen before; nothing written
    IGNORED_STALE = "ignored_stale"  # pending after terminal; nothing written
    IGNORED_REDUNDANT = "ignored_redundant"  # same terminal status, different event_id
    CONFLICT = "conflict"  # a different terminal after terminal; nothing written
