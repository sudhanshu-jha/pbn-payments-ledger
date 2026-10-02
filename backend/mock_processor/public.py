"""The processor's public API surface.

This is the ONLY module the ``payments`` app may import from ``mock_processor``.
It mirrors what a real processor SDK exposes: charge a token, ask for a
re-delivery of a charge's result. No models, no tables, no raw payment data.
"""

from dataclasses import dataclass

from . import services
from .delivery import sign_event, signed_payload


class ProcessorError(Exception):
    """Raised when the processor refuses or cannot complete a request."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ChargeResult:
    processor_reference: str
    method: str
    last4: str
    brand_or_bank_type: str


@dataclass(frozen=True)
class SignedEvent:
    """A webhook delivery as the processor would send it: raw bytes + signature."""

    body: bytes
    signature: str


def charge(payment_token: str, amount: int, currency: str) -> ChargeResult:
    """Charge a token. The outcome arrives later by webhook."""
    try:
        charge_obj = services.create_charge(payment_token, amount, currency)
    except services.ChargeError as exc:
        raise ProcessorError(exc.code) from exc
    token = charge_obj.token
    return ChargeResult(
        processor_reference=charge_obj.processor_reference,
        method=token.method,
        last4=token.last4,
        brand_or_bank_type=token.brand_or_bank_type,
    )


def request_redelivery(processor_reference: str) -> SignedEvent:
    """Re-issue the final event for a charge, byte-for-byte as a retry would.

    Same event_id, same body, same signature: the receiver's normal dedup
    rules apply, so re-requesting can never double-process.
    """
    charge_obj = services.ProcessorCharge.objects.filter(
        processor_reference=processor_reference
    ).first()
    if charge_obj is None:
        raise ProcessorError("unknown_reference")
    event = services.final_event(charge_obj)
    body = signed_payload(event)
    return SignedEvent(body=body, signature=sign_event(body))
