"""The payments app's view of the processor.

This is the only module in ``payments`` that may import from ``mock_processor``,
and it may import only ``mock_processor.public`` (the facade), never models.
A test enforces that. Swapping in an HTTP client is a one-class change here.
"""

from typing import Protocol

from mock_processor import public as processor  # noqa: I001 - the allowed boundary import


ProcessorError = processor.ProcessorError
ChargeResult = processor.ChargeResult
SignedEvent = processor.SignedEvent


class ProcessorGateway(Protocol):
    def charge(self, payment_token: str, amount: int, currency: str) -> ChargeResult: ...

    def request_redelivery(self, processor_reference: str) -> SignedEvent: ...


class InProcessGateway:
    """Calls the simulated processor in-process (allowed by the brief)."""

    def charge(self, payment_token: str, amount: int, currency: str) -> ChargeResult:
        return processor.charge(payment_token, amount, currency)

    def request_redelivery(self, processor_reference: str) -> SignedEvent:
        return processor.request_redelivery(processor_reference)


_gateway: ProcessorGateway = InProcessGateway()


def get_gateway() -> ProcessorGateway:
    return _gateway


def set_gateway(gateway: ProcessorGateway) -> None:
    """Test hook for substituting a fake processor."""
    global _gateway  # noqa: PLW0603
    _gateway = gateway
