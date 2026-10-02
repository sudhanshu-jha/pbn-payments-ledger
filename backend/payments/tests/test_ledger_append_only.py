from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from payments.enums import LedgerEventType, PaymentStatus
from payments.ledger import append_entry, derive_status, record_creation
from payments.models import LedgerEntry, LedgerImmutableError, Payment


class AppendOnlyLedgerTests(TestCase):
    def setUp(self):
        with transaction.atomic():
            self.payment = Payment.objects.create(amount=100, payment_token="tok_x")
            self.entry = record_creation(self.payment, occurred_at=timezone.now())

    # --- ORM guards -------------------------------------------------------

    def test_orm_update_is_refused(self):
        with self.assertRaises(LedgerImmutableError):
            LedgerEntry.objects.filter(pk=self.entry.pk).update(to_status="failed")

    def test_orm_delete_is_refused(self):
        with self.assertRaises(LedgerImmutableError):
            LedgerEntry.objects.filter(pk=self.entry.pk).delete()
        with self.assertRaises(LedgerImmutableError):
            self.entry.delete()

    def test_orm_resave_is_refused(self):
        self.entry.to_status = "failed"
        with self.assertRaises(LedgerImmutableError):
            self.entry.save()

    def test_bulk_update_is_refused(self):
        with self.assertRaises(LedgerImmutableError):
            LedgerEntry.objects.bulk_update([self.entry], ["to_status"])

    # --- database trigger: the actual guarantee ----------------------------

    def test_raw_sql_update_is_blocked_by_trigger(self):
        with (
            self.assertRaises(IntegrityError) as ctx,
            transaction.atomic(),
            connection.cursor() as cur,
        ):
            cur.execute(
                "UPDATE payments_ledgerentry SET to_status = 'failed' WHERE id = %s",
                [self.entry.pk],
            )
        self.assertIn("append-only", str(ctx.exception))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.to_status, PaymentStatus.PENDING)

    def test_raw_sql_delete_is_blocked_by_trigger(self):
        with (
            self.assertRaises(IntegrityError) as ctx,
            transaction.atomic(),
            connection.cursor() as cur,
        ):
            cur.execute("DELETE FROM payments_ledgerentry WHERE id = %s", [self.entry.pk])
        self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(LedgerEntry.objects.count(), 1)

    def test_deleting_a_payment_cannot_cascade_into_the_ledger(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.payment.delete()

    # --- database constraints ---------------------------------------------

    def test_at_most_one_terminal_entry_per_payment(self):
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(pk=self.payment.pk)
            append_entry(
                payment,
                event_type=LedgerEventType.PROCESSOR_WEBHOOK,
                to_status=PaymentStatus.SUCCEEDED,
                occurred_at=timezone.now(),
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(
                payment=self.payment,
                sequence=99,
                event_type=LedgerEventType.PROCESSOR_WEBHOOK,
                from_status=PaymentStatus.SUCCEEDED,
                to_status=PaymentStatus.FAILED,
                occurred_at=timezone.now(),
            )

    def test_sequence_is_unique_per_payment(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            LedgerEntry.objects.create(
                payment=self.payment,
                sequence=1,
                event_type=LedgerEventType.PAYMENT_CREATED,
                to_status=PaymentStatus.PENDING,
                occurred_at=timezone.now(),
            )

    # --- status is derivable from the ledger alone --------------------------

    def test_status_derives_from_ledger(self):
        self.assertEqual(derive_status(self.payment.ledger_entries.all()), ("pending", None))
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(pk=self.payment.pk)
            append_entry(
                payment,
                event_type=LedgerEventType.PROCESSOR_WEBHOOK,
                to_status=PaymentStatus.FAILED,
                failure_code="card_declined",
                occurred_at=timezone.now(),
            )
        self.payment.refresh_from_db()
        derived = derive_status(self.payment.ledger_entries.all())
        self.assertEqual(derived, ("failed", "card_declined"))
        self.assertEqual(derived, (self.payment.status, self.payment.failure_code))
