from django.test import SimpleTestCase

from payments.enums import PaymentStatus, WebhookOutcome
from payments.state_machine import decide, is_terminal


P, S, F = PaymentStatus.PENDING, PaymentStatus.SUCCEEDED, PaymentStatus.FAILED


class StateMachineTableTests(SimpleTestCase):
    """Every cell of the (current, incoming) table, as documented in the README."""

    def test_table(self):
        table = {
            (P, P): (WebhookOutcome.IGNORED_REDUNDANT, None),
            (P, S): (WebhookOutcome.APPLIED, S),
            (P, F): (WebhookOutcome.APPLIED, F),
            (S, P): (WebhookOutcome.IGNORED_STALE, None),
            (S, S): (WebhookOutcome.IGNORED_REDUNDANT, None),
            (S, F): (WebhookOutcome.CONFLICT, None),
            (F, P): (WebhookOutcome.IGNORED_STALE, None),
            (F, F): (WebhookOutcome.IGNORED_REDUNDANT, None),
            (F, S): (WebhookOutcome.CONFLICT, None),
        }
        for (current, incoming), (outcome, new_status) in table.items():
            with self.subTest(current=current, incoming=incoming):
                decision = decide(current, incoming)
                self.assertEqual(decision.outcome, outcome)
                self.assertEqual(decision.new_status, new_status)
                self.assertEqual(decision.applies, outcome == WebhookOutcome.APPLIED)

    def test_only_pending_to_terminal_applies(self):
        applied = {(c, i) for c in PaymentStatus for i in PaymentStatus if decide(c, i).applies}
        self.assertEqual(applied, {(P, S), (P, F)})

    def test_terminal_helper(self):
        self.assertFalse(is_terminal(P))
        self.assertTrue(is_terminal(S))
        self.assertTrue(is_terminal(F))

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            decide("pending", "refunded")
