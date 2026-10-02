"""Shared helpers for the payments test-suite."""

import json
import uuid

from django.test import Client

from mock_processor.delivery import SIGNATURE_HEADER, sign_event, signed_payload


TOKENIZE_URL = "/processor/tokenize"
PAYMENTS_URL = "/api/payments"
WEBHOOK_URL = "/webhooks/processor"

VISA_OK = "4242424242424242"
VISA_DECLINED = "4000000000000002"
VISA_PROCESSOR_ERROR = "4000000000000119"
VISA_SLOW = "4000000000000341"
BANK_ACCOUNT_OK = "000123456789"
BANK_ROUTING = "021000021"


def tokenize_card(client: Client, number=VISA_OK, expiry="12/30", cvv="123") -> str:
    response = client.post(
        TOKENIZE_URL,
        data={"number": number, "expiry": expiry, "cvv": cvv},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()["token"]


def tokenize_bank(
    client: Client, account_number=BANK_ACCOUNT_OK, routing_number=BANK_ROUTING
) -> str:
    response = client.post(
        TOKENIZE_URL,
        data={"account_number": account_number, "routing_number": routing_number},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()["token"]


def create_payment(client: Client, token: str, amount=1250, key=None, **overrides):
    body = {"amount": amount, "currency": "USD", "payment_token": token, **overrides}
    return client.post(
        PAYMENTS_URL,
        data=json.dumps(body),
        content_type="application/json",
        headers={"Idempotency-Key": key or f"key-{uuid.uuid4()}"},
    )


def make_event(processor_reference, status, failure_code=None, event_id=None, occurred_at=None):
    return {
        "event_id": event_id or f"evt_{uuid.uuid4().hex[:24]}",
        "processor_reference": processor_reference,
        "status": status,
        "failure_code": failure_code,
        "occurred_at": occurred_at or "2026-09-28T10:15:02.113Z",
    }


def post_webhook(client: Client, event: dict, signature=None, secret=None):
    body = signed_payload(event)
    if signature is None:
        signature = sign_event(body, secret)
    headers = {SIGNATURE_HEADER: signature} if signature is not False else {}
    return client.post(WEBHOOK_URL, data=body, content_type="application/json", headers=headers)


def processor_reference_for(payment_id) -> str:
    """Read the unmasked reference straight from our table (the API only shows it masked)."""
    from payments.models import Payment

    return Payment.objects.get(pk=payment_id).processor_reference
