from django.test import TestCase

from payments.enums import PaymentStatus, WebhookOutcome
from payments.models import LedgerEntry, Payment, WebhookEvent

from .helpers import (
    VISA_DECLINED,
    create_payment,
    make_event,
    post_webhook,
    processor_reference_for,
    tokenize_card,
)


class WebhookTestCase(TestCase):
    def setUp(self):
        token = tokenize_card(self.client)
        self.payment_id = create_payment(self.client, token).json()["id"]
        self.reference = processor_reference_for(self.payment_id)

    def payment(self):
        return Payment.objects.get(pk=self.payment_id)

    def ledger(self):
        return list(LedgerEntry.objects.filter(payment_id=self.payment_id).order_by("sequence"))

    def inbox(self):
        return list(WebhookEvent.objects.filter(payment_id=self.payment_id).order_by("received_at"))


class SignatureTests(WebhookTestCase):
    def test_missing_signature_is_rejected_and_changes_nothing(self):
        response = post_webhook(
            self.client, make_event(self.reference, "succeeded"), signature=False
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.payment().status, PaymentStatus.PENDING)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.inbox(), [])

    def test_wrong_signature_is_rejected_and_changes_nothing(self):
        response = post_webhook(
            self.client, make_event(self.reference, "succeeded"), signature="0" * 64
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.payment().status, PaymentStatus.PENDING)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.inbox(), [])

    def test_signature_with_wrong_secret_is_rejected(self):
        response = post_webhook(
            self.client, make_event(self.reference, "succeeded"), secret="not-the-secret"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.inbox(), [])

    def test_tampered_body_is_rejected(self):
        from mock_processor.delivery import SIGNATURE_HEADER, sign_event, signed_payload

        event = make_event(self.reference, "succeeded")
        signature = sign_event(signed_payload(event))
        tampered = signed_payload({**event, "status": "failed"})
        response = self.client.post(
            "/webhooks/processor",
            data=tampered,
            content_type="application/json",
            headers={SIGNATURE_HEADER: signature},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.payment().status, PaymentStatus.PENDING)

    def test_empty_secret_fails_closed(self):
        with self.settings(PROCESSOR_WEBHOOK_SECRET=""):
            response = post_webhook(self.client, make_event(self.reference, "succeeded"), secret="")
        self.assertEqual(response.status_code, 401)


class HappyPathTests(WebhookTestCase):
    def test_pending_then_succeeded(self):
        r1 = post_webhook(self.client, make_event(self.reference, "pending"))
        r2 = post_webhook(self.client, make_event(self.reference, "succeeded"))
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(r1.json()["outcome"], WebhookOutcome.IGNORED_REDUNDANT)
        self.assertEqual(r2.json()["outcome"], WebhookOutcome.APPLIED)
        self.assertEqual(self.payment().status, PaymentStatus.SUCCEEDED)
        entries = self.ledger()
        self.assertEqual(
            [(e.from_status, e.to_status) for e in entries],
            [(None, "pending"), ("pending", "succeeded")],
        )
        self.assertEqual(entries[-1].amount_delta, 1250)
        self.assertEqual(
            entries[-1].webhook_event.event_id, r2.json() and self.inbox()[-1].event_id
        )

    def test_failed_with_failure_code(self):
        response = post_webhook(
            self.client, make_event(self.reference, "failed", failure_code="card_declined")
        )
        self.assertEqual(response.status_code, 200)
        payment = self.payment()
        self.assertEqual((payment.status, payment.failure_code), ("failed", "card_declined"))
        self.assertEqual(self.ledger()[-1].failure_code, "card_declined")
        self.assertEqual(self.ledger()[-1].amount_delta, 0)

    def test_get_shows_ledger_history(self):
        post_webhook(self.client, make_event(self.reference, "succeeded"))
        data = self.client.get(f"/api/payments/{self.payment_id}").json()
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual([e["to_status"] for e in data["ledger"]], ["pending", "succeeded"])


class DuplicateAndOrderingTests(WebhookTestCase):
    def test_duplicate_event_id_writes_exactly_one_ledger_entry(self):
        event = make_event(self.reference, "succeeded")
        first = post_webhook(self.client, event)
        second = post_webhook(self.client, event)
        third = post_webhook(self.client, event)
        self.assertEqual([r.status_code for r in (first, second, third)], [200, 200, 200])
        self.assertEqual(first.json()["outcome"], WebhookOutcome.APPLIED)
        self.assertEqual(second.json()["outcome"], WebhookOutcome.DUPLICATE)
        self.assertEqual(third.json()["outcome"], WebhookOutcome.DUPLICATE)
        self.assertEqual(len(self.ledger()), 2)  # creation + exactly one transition
        inbox = self.inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].delivery_count, 3)

    def test_reverse_order_ends_in_final_status(self):
        final = post_webhook(self.client, make_event(self.reference, "succeeded"))
        late_pending = post_webhook(self.client, make_event(self.reference, "pending"))
        self.assertEqual(final.json()["outcome"], WebhookOutcome.APPLIED)
        self.assertEqual(late_pending.status_code, 200)
        self.assertEqual(late_pending.json()["outcome"], WebhookOutcome.IGNORED_STALE)
        self.assertEqual(self.payment().status, PaymentStatus.SUCCEEDED)
        self.assertEqual(len(self.ledger()), 2)
        self.assertEqual(
            [e.outcome for e in self.inbox()],
            [WebhookOutcome.APPLIED, WebhookOutcome.IGNORED_STALE],
        )

    def test_late_pending_after_failed_does_not_regress(self):
        post_webhook(
            self.client, make_event(self.reference, "failed", failure_code="processor_error")
        )
        post_webhook(self.client, make_event(self.reference, "pending"))
        self.assertEqual(self.payment().status, PaymentStatus.FAILED)
        self.assertEqual(self.payment().failure_code, "processor_error")

    def test_same_terminal_with_new_event_id_is_redundant_not_conflict(self):
        post_webhook(self.client, make_event(self.reference, "succeeded"))
        response = post_webhook(self.client, make_event(self.reference, "succeeded"))
        self.assertEqual(response.json()["outcome"], WebhookOutcome.IGNORED_REDUNDANT)
        self.assertEqual(len(self.ledger()), 2)


class ConflictingTerminalTests(WebhookTestCase):
    def test_succeeded_then_failed_keeps_succeeded_and_records_conflict(self):
        post_webhook(self.client, make_event(self.reference, "succeeded"))
        with self.assertLogs("payments.services.webhooks", level="ERROR") as logs:
            response = post_webhook(
                self.client, make_event(self.reference, "failed", failure_code="card_declined")
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], WebhookOutcome.CONFLICT)
        payment = self.payment()
        self.assertEqual((payment.status, payment.failure_code), ("succeeded", None))
        self.assertEqual(len(self.ledger()), 2)
        self.assertEqual(self.inbox()[-1].outcome, WebhookOutcome.CONFLICT)
        self.assertTrue(any("conflicting terminal" in line for line in logs.output))

    def test_failed_then_succeeded_keeps_failed(self):
        post_webhook(
            self.client, make_event(self.reference, "failed", failure_code="card_declined")
        )
        response = post_webhook(self.client, make_event(self.reference, "succeeded"))
        self.assertEqual(response.json()["outcome"], WebhookOutcome.CONFLICT)
        self.assertEqual(self.payment().status, PaymentStatus.FAILED)


class MalformedDeliveryTests(WebhookTestCase):
    def test_unknown_reference_is_404_and_writes_nothing(self):
        response = post_webhook(self.client, make_event("pr_unknown", "succeeded"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_unknown_status_is_400(self):
        response = post_webhook(self.client, make_event(self.reference, "refunded"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.payment().status, PaymentStatus.PENDING)

    def test_missing_field_is_400(self):
        event = make_event(self.reference, "succeeded")
        del event["event_id"]
        response = post_webhook(self.client, event)
        self.assertEqual(response.status_code, 400)
        self.assertIn("event_id", response.json()["detail"])

    def test_non_json_body_is_400(self):
        from mock_processor.delivery import SIGNATURE_HEADER, sign_event

        body = b"not json"
        response = self.client.post(
            "/webhooks/processor",
            data=body,
            content_type="application/json",
            headers={SIGNATURE_HEADER: sign_event(body)},
        )
        self.assertEqual(response.status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get("/webhooks/processor").status_code, 405)


class EndToEndScenarioTests(TestCase):
    def test_declined_card_via_simulated_processor(self):
        from mock_processor.delivery import DjangoClientTransport, deliver
        from mock_processor.models import ProcessorCharge
        from mock_processor.services import build_events

        token = tokenize_card(self.client, VISA_DECLINED)
        payment_id = create_payment(self.client, token).json()["id"]
        charge = ProcessorCharge.objects.get(
            processor_reference=processor_reference_for(payment_id)
        )
        deliver(build_events(charge), "normal", DjangoClientTransport())
        payment = Payment.objects.get(pk=payment_id)
        self.assertEqual((payment.status, payment.failure_code), ("failed", "card_declined"))
