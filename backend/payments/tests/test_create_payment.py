import json

from django.test import TestCase

from mock_processor.models import ProcessorCharge

from payments.enums import LedgerEventType, PaymentStatus
from payments.gateway import ProcessorError, get_gateway, set_gateway
from payments.models import IdempotencyKey, LedgerEntry, Payment

from .helpers import PAYMENTS_URL, VISA_DECLINED, create_payment, tokenize_bank, tokenize_card


class CreatePaymentTests(TestCase):
    def test_creates_pending_payment_and_submits_charge(self):
        token = tokenize_card(self.client)
        response = create_payment(self.client, token, amount=1250, key="k1")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["amount"], 1250)
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(
            (data["method"], data["last4"], data["brand_or_bank_type"]), ("card", "4242", "visa")
        )
        self.assertTrue(data["processor_reference"].startswith("pr_…"))
        self.assertEqual(len(data["ledger"]), 1)
        self.assertEqual(data["ledger"][0]["to_status"], "pending")
        self.assertEqual(data["ledger"][0]["event_type"], LedgerEventType.PAYMENT_CREATED)
        self.assertEqual(ProcessorCharge.objects.count(), 1)
        self.assertEqual(response["Idempotent-Replayed"], "false")

    def test_bank_token_works_too(self):
        token = tokenize_bank(self.client)
        response = create_payment(self.client, token)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((response.json()["method"], response.json()["last4"]), ("bank", "6789"))

    # --- idempotency ------------------------------------------------------

    def test_same_key_same_body_returns_original_response_and_creates_nothing(self):
        token = tokenize_card(self.client)
        first = create_payment(self.client, token, key="dup")
        second = create_payment(self.client, token, key="dup")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(ProcessorCharge.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.count(), 1)
        self.assertEqual(IdempotencyKey.objects.count(), 1)

    def test_same_key_different_body_is_rejected(self):
        token = tokenize_card(self.client)
        create_payment(self.client, token, amount=1250, key="dup")
        response = create_payment(self.client, token, amount=1251, key="dup")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "idempotency_key_reused")
        self.assertEqual(Payment.objects.count(), 1)

    def test_key_comparison_ignores_field_order(self):
        token = tokenize_card(self.client)
        a = self.client.post(
            PAYMENTS_URL,
            data=json.dumps({"amount": 5, "currency": "USD", "payment_token": token}),
            content_type="application/json",
            headers={"Idempotency-Key": "order"},
        )
        b = self.client.post(
            PAYMENTS_URL,
            data=json.dumps({"payment_token": token, "currency": "USD", "amount": 5}),
            content_type="application/json",
            headers={"Idempotency-Key": "order"},
        )
        self.assertEqual((a.status_code, b.status_code), (201, 201))
        self.assertEqual(a.json()["id"], b.json()["id"])

    def test_missing_key_is_rejected(self):
        token = tokenize_card(self.client)
        response = self.client.post(
            PAYMENTS_URL,
            data=json.dumps({"amount": 5, "currency": "USD", "payment_token": token}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "missing_idempotency_key")
        self.assertEqual(Payment.objects.count(), 0)

    def test_error_responses_are_replayed_too(self):
        response = create_payment(self.client, "tok_does_not_exist", key="bad")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "invalid_payment_token")
        again = create_payment(self.client, "tok_does_not_exist", key="bad")
        self.assertEqual(again.status_code, 422)
        self.assertEqual(again["Idempotent-Replayed"], "true")
        self.assertEqual(Payment.objects.count(), 0)

    # --- validation ----------------------------------------------------------

    def test_validation(self):
        token = tokenize_card(self.client)
        cases = [
            ({"amount": 0}, "amount"),
            ({"amount": -5}, "amount"),
            ({"amount": 12.5}, "amount"),
            ({"currency": "EUR"}, "currency"),
            ({"payment_token": ""}, "payment_token"),
        ]
        for overrides, field in cases:
            with self.subTest(overrides=overrides):
                response = create_payment(self.client, token, **overrides)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json()["detail"])
        self.assertEqual(Payment.objects.count(), 0)

    def test_card_data_is_rejected_at_the_payments_api(self):
        token = tokenize_card(self.client)
        response = create_payment(self.client, token, number="4242424242424242", cvv="123")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()["detail"]), {"number", "cvv"})
        self.assertEqual(Payment.objects.count(), 0)

    def test_single_use_token_cannot_be_charged_twice_with_different_keys(self):
        token = tokenize_card(self.client)
        self.assertEqual(create_payment(self.client, token, key="a").status_code, 201)
        response = create_payment(self.client, token, key="b")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "invalid_payment_token")

    # --- processor outage ---------------------------------------------------

    def test_processor_outage_is_recorded_honestly_as_failed(self):
        class DownGateway:
            def charge(self, *a, **k):
                raise ProcessorError("timeout")

            def request_redelivery(self, *a, **k):
                raise ProcessorError("timeout")

        original = get_gateway()
        set_gateway(DownGateway())
        try:
            token = tokenize_card(self.client, VISA_DECLINED)
            response = create_payment(self.client, token, key="down")
        finally:
            set_gateway(original)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], PaymentStatus.FAILED)
        self.assertEqual(response.json()["failure_code"], "processor_error")
        self.assertEqual(
            [e["event_type"] for e in response.json()["ledger"]],
            [LedgerEventType.PAYMENT_CREATED, LedgerEventType.PROCESSOR_UNAVAILABLE],
        )

    # --- retrieval -------------------------------------------------------------

    def test_get_detail_and_list(self):
        token = tokenize_card(self.client)
        payment_id = create_payment(self.client, token).json()["id"]
        detail = self.client.get(f"{PAYMENTS_URL}/{payment_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], payment_id)
        self.assertNotIn("payment_token", detail.json())
        listing = self.client.get(PAYMENTS_URL)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([p["id"] for p in listing.json()], [payment_id])

    def test_get_unknown_payment(self):
        response = self.client.get(f"{PAYMENTS_URL}/00000000-0000-0000-0000-000000000000")
        self.assertEqual(response.status_code, 404)
