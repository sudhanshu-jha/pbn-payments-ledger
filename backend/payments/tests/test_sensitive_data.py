"""After a full payment flow, no full card number, CVV or account number may
appear anywhere in the database or in anything that was logged."""

import logging

from django.db import connection
from django.test import TestCase

from mock_processor.delivery import DjangoClientTransport, deliver
from mock_processor.models import ProcessorCharge
from mock_processor.services import build_events

from payments.logging_filters import SensitiveDataFilter, scrub

from .helpers import create_payment, processor_reference_for, tokenize_bank, tokenize_card


CARD_NUMBER = "4000000000000341"  # distinctive, Luhn-valid, slow-settle scenario
CVV = "987"
ACCOUNT_NUMBER = "99887766554433"  # 14 digits, distinctive
ROUTING_NUMBER = "021000021"


class CapturingHandler(logging.Handler):
    """Captures the *raw* records (before the scrubber), so the test proves the
    code itself never logs sensitive values, not just that a filter hides them."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def dump_every_table() -> str:
    """Concatenate every value in every table of the test database."""
    chunks = []
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names(cursor):
            cursor.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - introspected table name
            for row in cursor.fetchall():
                chunks.append(" ".join(str(value) for value in row))
    return "\n".join(chunks)


class NoSensitiveDataTests(TestCase):
    def setUp(self):
        self.handler = CapturingHandler()
        logging.getLogger().addHandler(self.handler)
        self.addCleanup(logging.getLogger().removeHandler, self.handler)
        logging.getLogger().setLevel(logging.DEBUG)

    def run_full_flow(self):
        card_token = tokenize_card(self.client, CARD_NUMBER, cvv=CVV)
        bank_token = tokenize_bank(self.client, ACCOUNT_NUMBER, ROUTING_NUMBER)
        card_payment = create_payment(self.client, card_token).json()["id"]
        bank_payment = create_payment(self.client, bank_token).json()["id"]
        for payment_id in (card_payment, bank_payment):
            charge = ProcessorCharge.objects.get(
                processor_reference=processor_reference_for(payment_id)
            )
            deliver(build_events(charge), "duplicate", DjangoClientTransport())
        # Also exercise the rejection paths, which log too.
        self.client.post(
            "/processor/tokenize",
            data={"number": CARD_NUMBER[:-1] + "0", "expiry": "12/30", "cvv": CVV},
            content_type="application/json",
        )
        return card_payment, bank_payment

    def test_nothing_sensitive_in_database_or_logs(self):
        card_payment, bank_payment = self.run_full_flow()

        db_dump = dump_every_table()
        self.assertNotIn(CARD_NUMBER, db_dump)
        self.assertNotIn(ACCOUNT_NUMBER, db_dump)
        self.assertNotIn(ROUTING_NUMBER, db_dump)
        # last4 IS allowed to be stored
        self.assertIn("0341", db_dump)

        logged = "\n".join(self.handler.records)
        self.assertNotIn(CARD_NUMBER, logged)
        self.assertNotIn(ACCOUNT_NUMBER, logged)
        self.assertNotIn(ROUTING_NUMBER, logged)
        self.assertNotIn("cvv", logged.lower())
        self.assertTrue(logged, "expected the flow to produce some log output")

        for payment_id in (card_payment, bank_payment):
            body = self.client.get(f"/api/payments/{payment_id}").content.decode()
            self.assertNotIn(CARD_NUMBER, body)
            self.assertNotIn(ACCOUNT_NUMBER, body)
            self.assertNotIn("tok_", body)

    def test_api_responses_mask_processor_reference(self):
        card_payment, _ = self.run_full_flow()
        full_reference = processor_reference_for(card_payment)
        body = self.client.get(f"/api/payments/{card_payment}").content.decode()
        self.assertNotIn(full_reference, body)
        self.assertIn(full_reference[-4:], body)


class SensitiveDataFilterTests(TestCase):
    def test_scrubs_pans_and_cvvs(self):
        self.assertEqual(scrub("card 4242424242424242 used"), "card [REDACTED] used")
        self.assertEqual(scrub("card 4242 4242 4242 4242 used"), "card [REDACTED] used")
        self.assertEqual(scrub("acct 99887766554433"), "acct [REDACTED]")
        self.assertEqual(scrub('{"cvv": "123"}'), '{"cvv": "[REDACTED]"}')
        self.assertEqual(scrub("cvc=9876"), "cvc=[REDACTED]")

    def test_leaves_short_numbers_alone(self):
        self.assertEqual(scrub("last4 4242 amount 1250"), "last4 4242 amount 1250")

    def test_filter_rewrites_record_message_and_args(self):
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "pan=%s", ("4242424242424242",), None
        )
        SensitiveDataFilter().filter(record)
        self.assertEqual(record.getMessage(), "pan=[REDACTED]")
