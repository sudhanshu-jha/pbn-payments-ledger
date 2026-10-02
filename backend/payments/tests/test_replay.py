from django.test import TestCase

from model_bakery import baker

from payments.enums import PaymentStatus, WebhookOutcome
from payments.models import LedgerEntry, Payment

from .helpers import (
    create_payment,
    make_event,
    post_webhook,
    processor_reference_for,
    tokenize_card,
)


class ReplayEndpointTests(TestCase):
    def setUp(self):
        token = tokenize_card(self.client)
        self.payment_id = create_payment(self.client, token).json()["id"]
        self.url = f"/api/payments/{self.payment_id}/replay"
        self.admin = baker.make("users.User", is_staff=True, is_superuser=True)
        self.user = baker.make("users.User")

    def test_anonymous_and_non_admin_are_refused(self):
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertEqual(Payment.objects.get(pk=self.payment_id).status, PaymentStatus.PENDING)

    def test_replay_resolves_a_stuck_payment(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["outcome"], WebhookOutcome.APPLIED)
        self.assertEqual(Payment.objects.get(pk=self.payment_id).status, PaymentStatus.SUCCEEDED)

    def test_replay_is_idempotent_with_the_real_webhook(self):
        """Replaying then receiving the real delivery (or vice versa) writes one entry."""
        self.client.force_login(self.admin)
        first = self.client.post(self.url)
        self.assertEqual(first.json()["outcome"], WebhookOutcome.APPLIED)
        second = self.client.post(self.url)
        self.assertEqual(second.status_code, 409)  # already terminal
        transitions = LedgerEntry.objects.filter(
            payment_id=self.payment_id, from_status__isnull=False
        )
        self.assertEqual(transitions.count(), 1)

    def test_replay_refuses_terminal_payments(self):
        post_webhook(
            self.client,
            make_event(processor_reference_for(self.payment_id), "failed", "card_declined"),
        )
        self.client.force_login(self.admin)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "not_pending")
