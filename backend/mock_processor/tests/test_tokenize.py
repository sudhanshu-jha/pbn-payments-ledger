from django.test import TestCase

from mock_processor.models import ProcessorToken


TOKENIZE = "/processor/tokenize"


class TokenizeViewTests(TestCase):
    def post(self, body):
        return self.client.post(TOKENIZE, data=body, content_type="application/json")

    def test_card_success_returns_token_and_last4_only(self):
        response = self.post({"number": "4242 4242 4242 4242", "expiry": "12/30", "cvv": "123"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(set(data), {"token", "method", "last4", "brand_or_bank_type"})
        self.assertTrue(data["token"].startswith("tok_"))
        self.assertEqual(data["method"], "card")
        self.assertEqual(data["last4"], "4242")
        self.assertEqual(data["brand_or_bank_type"], "visa")

    def test_bank_success(self):
        response = self.post({"account_number": "000123456789", "routing_number": "021000021"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["method"], "bank")
        self.assertEqual(data["last4"], "6789")
        self.assertEqual(data["brand_or_bank_type"], "checking")

    def test_luhn_failure(self):
        response = self.post({"number": "4242424242424241", "expiry": "12/30", "cvv": "123"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_number"})

    def test_expired_card(self):
        response = self.post({"number": "4242424242424242", "expiry": "01/20", "cvv": "123"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_expiry"})

    def test_bad_cvv(self):
        response = self.post({"number": "4242424242424242", "expiry": "12/30", "cvv": "12"})
        self.assertEqual(response.json(), {"error": "invalid_cvv"})

    def test_bad_routing_number(self):
        response = self.post({"account_number": "000123456789", "routing_number": "12345678"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_routing_number"})

    def test_neither_card_nor_bank(self):
        response = self.post({"hello": "world"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_request"})

    def test_processor_persists_only_last4_and_decision(self):
        self.post({"number": "4000000000000002", "expiry": "12/30", "cvv": "999"})
        token = ProcessorToken.objects.get()
        self.assertEqual(token.last4, "0002")
        self.assertEqual(token.final_status, "failed")
        self.assertEqual(token.failure_code, "card_declined")
        stored = {str(getattr(token, f.name)) for f in ProcessorToken._meta.fields}
        self.assertFalse(any("4000000000000002" in v for v in stored))
        self.assertFalse(any("999" == v for v in stored))

    def test_scenarios_from_last4(self):
        cases = [
            (
                {"number": "4000000000000002", "expiry": "12/30", "cvv": "123"},
                "failed",
                "card_declined",
                0,
            ),
            (
                {"account_number": "99990002", "routing_number": "021000021"},
                "failed",
                "insufficient_funds",
                0,
            ),
            (
                {"number": "4000000000000119", "expiry": "12/30", "cvv": "123"},
                "failed",
                "processor_error",
                0,
            ),
            (
                {"number": "4000000000000341", "expiry": "12/30", "cvv": "123"},
                "succeeded",
                None,
                15,
            ),
            ({"number": "4242424242424242", "expiry": "12/30", "cvv": "123"}, "succeeded", None, 0),
        ]
        for body, status, code, settle in cases:
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 200, response.content)
                token = ProcessorToken.objects.get(token=response.json()["token"])
                self.assertEqual(
                    (token.final_status, token.failure_code, token.settle_after_seconds),
                    (status, code, settle),
                )
