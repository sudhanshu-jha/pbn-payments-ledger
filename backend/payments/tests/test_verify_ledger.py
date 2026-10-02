from io import StringIO

from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase

from payments.models import Payment

from .helpers import (
    create_payment,
    make_event,
    post_webhook,
    processor_reference_for,
    tokenize_card,
)


class VerifyLedgerCommandTests(TestCase):
    def test_consistent_ledger_passes(self):
        token = tokenize_card(self.client)
        payment_id = create_payment(self.client, token).json()["id"]
        post_webhook(self.client, make_event(processor_reference_for(payment_id), "succeeded"))
        out = StringIO()
        call_command("verify_ledger", stdout=out)
        self.assertIn("ok: 1 payment(s) consistent", out.getvalue())

    def test_detects_cache_drift(self):
        token = tokenize_card(self.client)
        payment_id = create_payment(self.client, token).json()["id"]
        # Corrupt the cache behind the ORM's back (the ledger itself cannot be touched).
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payments_payment SET status = 'succeeded' WHERE id = %s", [payment_id]
            )
        with self.assertRaises(CommandError):
            call_command("verify_ledger", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(Payment.objects.get(pk=payment_id).status, "succeeded")
