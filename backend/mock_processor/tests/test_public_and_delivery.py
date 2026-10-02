import hashlib
import hmac
import json

from django.test import TestCase, override_settings

from mock_processor import public
from mock_processor.delivery import (
    MODE_CONCURRENT,
    MODE_DUPLICATE,
    MODE_NORMAL,
    MODE_REVERSE,
    deliver,
    sign_event,
    signed_payload,
)
from mock_processor.models import ProcessorCharge
from mock_processor.services import build_events, tokenize


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, body, signature):
        self.calls.append((json.loads(body), signature))
        from mock_processor.delivery import DeliveryResult

        return DeliveryResult(json.loads(body)["event_id"], 200, "{}")


class PublicFacadeTests(TestCase):
    def test_charge_returns_reference_and_masked_details(self):
        token = tokenize({"number": "4242424242424242", "expiry": "12/30", "cvv": "123"}).token
        result = public.charge(token, 500, "USD")
        self.assertTrue(result.processor_reference.startswith("pr_"))
        self.assertEqual(
            (result.method, result.last4, result.brand_or_bank_type), ("card", "4242", "visa")
        )
        self.assertEqual(ProcessorCharge.objects.count(), 1)

    def test_token_is_single_use(self):
        token = tokenize({"number": "4242424242424242", "expiry": "12/30", "cvv": "123"}).token
        public.charge(token, 500, "USD")
        with self.assertRaises(public.ProcessorError) as ctx:
            public.charge(token, 500, "USD")
        self.assertEqual(ctx.exception.code, "invalid_token")

    def test_unknown_token(self):
        with self.assertRaises(public.ProcessorError) as ctx:
            public.charge("tok_nope", 500, "USD")
        self.assertEqual(ctx.exception.code, "invalid_token")

    def test_redelivery_is_byte_identical_to_original_final_event(self):
        token = tokenize({"number": "4242424242424242", "expiry": "12/30", "cvv": "123"}).token
        ref = public.charge(token, 500, "USD").processor_reference
        charge = ProcessorCharge.objects.get(processor_reference=ref)
        original = signed_payload(build_events(charge)[1])
        redelivered = public.request_redelivery(ref)
        self.assertEqual(redelivered.body, original)
        self.assertEqual(redelivered.signature, sign_event(original))

    def test_redelivery_unknown_reference(self):
        with self.assertRaises(public.ProcessorError):
            public.request_redelivery("pr_missing")


@override_settings(PROCESSOR_WEBHOOK_SECRET="s3cret")
class SigningTests(TestCase):
    def test_signature_is_hex_hmac_sha256_of_raw_body(self):
        body = b'{"a":1}'
        expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(sign_event(body), expected)


class DeliveryModeTests(TestCase):
    def setUp(self):
        token = tokenize({"number": "4242424242424242", "expiry": "12/30", "cvv": "123"}).token
        ref = public.charge(token, 500, "USD").processor_reference
        self.charge = ProcessorCharge.objects.get(processor_reference=ref)
        self.events = build_events(self.charge)
        self.transport = RecordingTransport()

    def statuses(self):
        return [event["status"] for event, _ in self.transport.calls]

    def test_events_have_distinct_deterministic_ids(self):
        pending, final = self.events
        self.assertNotEqual(pending["event_id"], final["event_id"])
        self.assertEqual(self.events, build_events(self.charge))
        self.assertEqual(final["status"], "succeeded")
        self.assertIsNone(final["failure_code"])

    def test_normal(self):
        deliver(self.events, MODE_NORMAL, self.transport)
        self.assertEqual(self.statuses(), ["pending", "succeeded"])

    def test_duplicate(self):
        deliver(self.events, MODE_DUPLICATE, self.transport)
        self.assertEqual(self.statuses(), ["pending", "pending", "succeeded", "succeeded"])
        ids = [e["event_id"] for e, _ in self.transport.calls]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[2], ids[3])

    def test_reverse(self):
        deliver(self.events, MODE_REVERSE, self.transport)
        self.assertEqual(self.statuses(), ["succeeded", "pending"])

    def test_concurrent_sends_two_copies_of_final(self):
        deliver(self.events, MODE_CONCURRENT, self.transport)
        self.assertEqual(sorted(self.statuses()), ["pending", "succeeded", "succeeded"])
        finals = [e for e, _ in self.transport.calls if e["status"] == "succeeded"]
        self.assertEqual(finals[0], finals[1])

    def test_settlement_delay_is_honoured_via_sleep(self):
        slept = []
        deliver(
            self.events, MODE_NORMAL, self.transport, sleep=slept.append, settle_after_seconds=15
        )
        self.assertEqual(slept, [15])

    def test_every_delivery_is_signed(self):
        deliver(self.events, MODE_NORMAL, self.transport)
        for event, signature in self.transport.calls:
            self.assertEqual(signature, sign_event(signed_payload(event)))
