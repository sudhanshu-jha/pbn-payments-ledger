"""Processor-internal operations: tokenize raw payment details, create charges,
and build the webhook events a charge will emit.
"""

from dataclasses import dataclass
from datetime import UTC, timedelta

from django.db import transaction
from django.utils import timezone

from . import validators
from .models import ProcessorCharge, ProcessorToken
from .scenarios import METHOD_BANK, METHOD_CARD, STATUS_PENDING, scenario_for


class TokenizationError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ChargeError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TokenizeResult:
    token: str
    method: str
    last4: str
    brand_or_bank_type: str

    def as_dict(self) -> dict:
        return {
            "token": self.token,
            "method": self.method,
            "last4": self.last4,
            "brand_or_bank_type": self.brand_or_bank_type,
        }


def tokenize(data: dict) -> TokenizeResult:
    """Validate raw card/bank details and exchange them for a token.

    The raw values live only in this frame; nothing but ``last4`` is persisted.
    """
    if "number" in data:
        return _tokenize_card(
            number=str(data.get("number") or ""),
            expiry=str(data.get("expiry") or ""),
            cvv=str(data.get("cvv") or ""),
        )
    if "account_number" in data:
        return _tokenize_bank(
            account_number=str(data.get("account_number") or ""),
            routing_number=str(data.get("routing_number") or ""),
        )
    raise TokenizationError("invalid_request")


def _tokenize_card(number: str, expiry: str, cvv: str) -> TokenizeResult:
    number = number.replace(" ", "")
    if not validators.card_number_valid(number):
        raise TokenizationError("invalid_number")
    if not validators.expiry_valid(expiry):
        raise TokenizationError("invalid_expiry")
    if not validators.cvv_valid(cvv):
        raise TokenizationError("invalid_cvv")
    return _store_token(METHOD_CARD, number[-4:], validators.card_brand(number))


def _tokenize_bank(account_number: str, routing_number: str) -> TokenizeResult:
    if not validators.routing_number_valid(routing_number):
        raise TokenizationError("invalid_routing_number")
    if not validators.account_number_valid(account_number):
        raise TokenizationError("invalid_account_number")
    return _store_token(METHOD_BANK, account_number[-4:], "checking")


def _store_token(method: str, last4: str, brand_or_bank_type: str) -> TokenizeResult:
    scenario = scenario_for(method, last4)
    token = ProcessorToken.objects.create(
        token=ProcessorToken.generate_token(),
        method=method,
        last4=last4,
        brand_or_bank_type=brand_or_bank_type,
        final_status=scenario.final_status,
        failure_code=scenario.failure_code,
        settle_after_seconds=scenario.settle_after_seconds,
    )
    return TokenizeResult(token.token, token.method, token.last4, token.brand_or_bank_type)


def create_charge(token_value: str, amount: int, currency: str) -> ProcessorCharge:
    """Charge a single-use token. Raises ChargeError('invalid_token') if unknown/used."""
    with transaction.atomic():
        token = (
            ProcessorToken.objects.select_for_update()
            .filter(token=token_value, used_at__isnull=True)
            .first()
        )
        if token is None:
            raise ChargeError("invalid_token")
        if currency != "USD":
            raise ChargeError("unsupported_currency")
        if amount <= 0:
            raise ChargeError("invalid_amount")
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        return ProcessorCharge.objects.create(
            processor_reference=ProcessorCharge.generate_reference(),
            token=token,
            amount=amount,
            currency=currency,
            final_status=token.final_status,
            failure_code=token.failure_code,
            settle_after_seconds=token.settle_after_seconds,
        )


def _event_id(reference: str, status: str) -> str:
    # Deterministic per (charge, status): a retried delivery carries the SAME
    # event_id, exactly like a real processor's retry would.
    import hashlib

    digest = hashlib.sha256(f"{reference}:{status}".encode()).hexdigest()
    return f"evt_{digest[:24]}"


def build_events(charge: ProcessorCharge) -> list[dict]:
    """The ordered [pending, final] events this charge emits."""
    pending_at = charge.created_at
    final_at = pending_at + timedelta(seconds=charge.settle_after_seconds)
    return [
        {
            "event_id": _event_id(charge.processor_reference, STATUS_PENDING),
            "processor_reference": charge.processor_reference,
            "status": STATUS_PENDING,
            "failure_code": None,
            "occurred_at": _iso(pending_at),
        },
        {
            "event_id": _event_id(charge.processor_reference, charge.final_status),
            "processor_reference": charge.processor_reference,
            "status": charge.final_status,
            "failure_code": charge.failure_code,
            "occurred_at": _iso(final_at),
        },
    ]


def final_event(charge: ProcessorCharge) -> dict:
    return build_events(charge)[1]


def _iso(dt) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
