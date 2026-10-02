"""Truly concurrent webhook deliveries on separate database connections.

These tests deliberately use ``TransactionTestCase``: each test commits for
real so two worker threads, each with its own Postgres connection, can race
against each other through the actual HTTP handler. A test inside a single
transaction could never exhibit (or prove the absence of) this race.
"""

import threading

from django.db import connection
from django.test import Client, TransactionTestCase

from mock_processor.delivery import SIGNATURE_HEADER, sign_event, signed_payload

from payments.enums import PaymentStatus, WebhookOutcome
from payments.models import LedgerEntry, Payment, WebhookEvent

from .helpers import WEBHOOK_URL, create_payment, make_event, processor_reference_for, tokenize_card


def fire_concurrently(events: list[dict]) -> list[dict]:
    """POST every event from its own thread, released by a barrier at the same instant.

    Each thread gets its own DB connection (Django connections are
    thread-local) and closes it on exit.
    """
    barrier = threading.Barrier(len(events))
    results: list[dict | None] = [None] * len(events)
    errors: list[BaseException] = []

    def worker(index: int, event: dict):
        try:
            body = signed_payload(event)
            headers = {SIGNATURE_HEADER: sign_event(body)}
            barrier.wait(timeout=5)
            response = Client().post(
                WEBHOOK_URL, data=body, content_type="application/json", headers=headers
            )
            results[index] = {"status_code": response.status_code, **response.json()}
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, e)) for i, e in enumerate(events)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    if errors:
        raise errors[0]
    assert all(r is not None for r in results), "a worker thread did not finish"
    return results  # type: ignore[return-value]


class ConcurrentWebhookTests(TransactionTestCase):
    def setUp(self):
        client = Client()
        token = tokenize_card(client)
        self.payment_id = create_payment(client, token).json()["id"]
        self.reference = processor_reference_for(self.payment_id)

    def transitions(self):
        return list(
            LedgerEntry.objects.filter(payment_id=self.payment_id, from_status__isnull=False)
        )

    def test_two_concurrent_copies_of_the_same_final_event(self):
        """The brief's headline case: identical final event, delivered twice at once."""
        event = make_event(self.reference, "succeeded")
        results = fire_concurrently([event, event])

        self.assertEqual([r["status_code"] for r in results], [200, 200])
        self.assertEqual(
            sorted(r["outcome"] for r in results),
            sorted([WebhookOutcome.APPLIED, WebhookOutcome.DUPLICATE]),
        )
        payment = Payment.objects.get(pk=self.payment_id)
        self.assertEqual(payment.status, PaymentStatus.SUCCEEDED)
        self.assertEqual(len(self.transitions()), 1)
        inbox = WebhookEvent.objects.filter(payment_id=self.payment_id)
        self.assertEqual(inbox.count(), 1)
        self.assertEqual(inbox.get().delivery_count, 2)

    def test_concurrent_conflicting_terminals_produce_exactly_one_terminal_entry(self):
        """Different event_ids, opposite outcomes, same instant: the row lock serialises
        them and exactly one wins; the other is recorded as a conflict."""
        results = fire_concurrently(
            [
                make_event(self.reference, "succeeded"),
                make_event(self.reference, "failed", failure_code="card_declined"),
            ]
        )
        self.assertEqual([r["status_code"] for r in results], [200, 200])
        self.assertEqual(
            sorted(r["outcome"] for r in results),
            sorted([WebhookOutcome.APPLIED, WebhookOutcome.CONFLICT]),
        )
        payment = Payment.objects.get(pk=self.payment_id)
        self.assertIn(payment.status, (PaymentStatus.SUCCEEDED, PaymentStatus.FAILED))
        transitions = self.transitions()
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].to_status, payment.status)
        self.assertEqual(WebhookEvent.objects.filter(payment_id=self.payment_id).count(), 2)

    def test_concurrent_pending_and_final_never_regress(self):
        """Pending and final racing: whichever order the lock picks, the end state is final."""
        results = fire_concurrently(
            [make_event(self.reference, "pending"), make_event(self.reference, "succeeded")]
        )
        self.assertEqual([r["status_code"] for r in results], [200, 200])
        payment = Payment.objects.get(pk=self.payment_id)
        self.assertEqual(payment.status, PaymentStatus.SUCCEEDED)
        self.assertEqual(len(self.transitions()), 1)

    def test_many_concurrent_duplicates(self):
        """Five copies at once still yield one transition."""
        event = make_event(self.reference, "succeeded")
        results = fire_concurrently([event] * 5)
        self.assertEqual([r["status_code"] for r in results], [200] * 5)
        self.assertEqual(sum(r["outcome"] == WebhookOutcome.APPLIED for r in results), 1)
        self.assertEqual(len(self.transitions()), 1)
        self.assertEqual(WebhookEvent.objects.get(payment_id=self.payment_id).delivery_count, 5)


class ConcurrentCreateTests(TransactionTestCase):
    def test_same_idempotency_key_at_the_same_instant_creates_one_payment(self):
        token = tokenize_card(Client())
        body = {"amount": 900, "currency": "USD", "payment_token": token}
        barrier = threading.Barrier(2)
        results: list[tuple[int, dict] | None] = [None, None]

        def worker(index):
            import json

            try:
                barrier.wait(timeout=5)
                response = Client().post(
                    "/api/payments",
                    data=json.dumps(body),
                    content_type="application/json",
                    headers={"Idempotency-Key": "race"},
                )
                results[index] = (response.status_code, response.json())
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertTrue(all(results))
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [201, 201])
        self.assertEqual(results[0][1]["id"], results[1][1]["id"])
        self.assertEqual(Payment.objects.count(), 1)
