"""verify_ledger: assert every Payment.status equals the status derived from its ledger."""

from django.core.management.base import BaseCommand, CommandError

from payments.ledger import derive_status
from payments.models import Payment


class Command(BaseCommand):
    help = "Check that each payment's cached status matches its append-only ledger."

    def handle(self, *args, **options):
        mismatches = []
        for payment in Payment.objects.prefetch_related("ledger_entries"):
            derived_status, derived_code = derive_status(payment.ledger_entries.all())
            if (derived_status, derived_code) != (payment.status, payment.failure_code):
                mismatches.append((payment.id, payment.status, derived_status))
        if mismatches:
            for payment_id, cached, derived in mismatches:
                self.stderr.write(f"{payment_id}: cached={cached} ledger={derived}")
            raise CommandError(f"{len(mismatches)} payment(s) disagree with their ledger")
        self.stdout.write(
            self.style.SUCCESS(f"ok: {Payment.objects.count()} payment(s) consistent")
        )
