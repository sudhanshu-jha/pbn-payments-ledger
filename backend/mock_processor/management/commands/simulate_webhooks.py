"""simulate_webhooks --reference <processor_reference> --mode <mode>

Replays a charge's webhook events to the payments service the way a real
processor would under bad conditions: retried, reordered, or concurrent.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mock_processor.delivery import MODES, HttpTransport, deliver
from mock_processor.models import ProcessorCharge
from mock_processor.services import build_events


class Command(BaseCommand):
    help = "Deliver a charge's webhook events in a chosen messy-delivery mode."

    def add_arguments(self, parser):
        parser.add_argument("--reference", required=True, help="processor_reference of the charge")
        parser.add_argument("--mode", required=True, choices=MODES)
        parser.add_argument(
            "--url",
            default=None,
            help=f"Webhook URL (default: settings.PROCESSOR_WEBHOOK_URL = {settings.PROCESSOR_WEBHOOK_URL})",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="Skip the settlement delay for slow-settling test values (e.g. last4 0341).",
        )

    def handle(self, *args, **options):
        charge = ProcessorCharge.objects.filter(processor_reference=options["reference"]).first()
        if charge is None:
            raise CommandError(f"unknown processor_reference {options['reference']!r}")

        transport = HttpTransport(options["url"] or settings.PROCESSOR_WEBHOOK_URL)
        settle = 0 if options["fast"] else charge.settle_after_seconds
        self.stdout.write(
            f"delivering {charge.processor_reference} in mode={options['mode']} "
            f"(final={charge.final_status}, settle_after={settle}s) -> {transport.url}"
        )
        results = deliver(
            build_events(charge), options["mode"], transport, settle_after_seconds=settle
        )
        for result in results:
            self.stdout.write(f"  {result.event_id}: HTTP {result.status_code} {result.body}")
