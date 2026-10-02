"""Outcome table from the assignment brief, keyed on the last 4 digits.

The processor decides the outcome at tokenization time (from the last 4) and
persists only the *decision*, never the full number.
"""

from dataclasses import dataclass


METHOD_CARD = "card"
METHOD_BANK = "bank"

STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

FAILURE_CARD_DECLINED = "card_declined"
FAILURE_INSUFFICIENT_FUNDS = "insufficient_funds"
FAILURE_PROCESSOR_ERROR = "processor_error"


@dataclass(frozen=True)
class Scenario:
    final_status: str
    failure_code: str | None
    settle_after_seconds: int


def scenario_for(method: str, last4: str) -> Scenario:
    if last4 == "0002":
        code = FAILURE_CARD_DECLINED if method == METHOD_CARD else FAILURE_INSUFFICIENT_FUNDS
        return Scenario(STATUS_FAILED, code, 0)
    if last4 == "0119":
        return Scenario(STATUS_FAILED, FAILURE_PROCESSOR_ERROR, 0)
    if last4 == "0341":
        return Scenario(STATUS_SUCCEEDED, None, 15)
    return Scenario(STATUS_SUCCEEDED, None, 0)
