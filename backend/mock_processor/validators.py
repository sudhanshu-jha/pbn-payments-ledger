"""Input validation for the simulated processor's tokenization endpoint.

Everything here operates on raw card/bank data. That data must never leave this
app: callers receive a token, never the number back.
"""

import re
from datetime import date


DIGITS_ONLY = re.compile(r"^\d+$")
EXPIRY_RE = re.compile(r"^(?P<month>\d{2})/(?P<year>\d{2}|\d{4})$")


def luhn_valid(number: str) -> bool:
    """Return True if ``number`` passes the Luhn checksum (ISO/IEC 7812-1)."""
    if not number or not DIGITS_ONLY.match(number):
        return False
    total = 0
    for index, digit in enumerate(reversed(number)):
        value = int(digit)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def card_number_valid(number: str) -> bool:
    return 12 <= len(number) <= 19 and luhn_valid(number)


def expiry_valid(expiry: str, today: date | None = None) -> bool:
    """``MM/YY`` or ``MM/YYYY``; valid through the last day of that month."""
    match = EXPIRY_RE.match(expiry or "")
    if not match:
        return False
    month = int(match.group("month"))
    year = int(match.group("year"))
    if len(match.group("year")) == 2:
        year += 2000
    if not 1 <= month <= 12:
        return False
    today = today or date.today()
    return (year, month) >= (today.year, today.month)


def cvv_valid(cvv: str) -> bool:
    return bool(cvv) and DIGITS_ONLY.match(cvv) is not None and 3 <= len(cvv) <= 4


def routing_number_valid(routing_number: str) -> bool:
    return (
        bool(routing_number)
        and DIGITS_ONLY.match(routing_number) is not None
        and len(routing_number) == 9
    )


def account_number_valid(account_number: str) -> bool:
    return (
        bool(account_number)
        and DIGITS_ONLY.match(account_number) is not None
        and 4 <= len(account_number) <= 17
    )


def card_brand(number: str) -> str:
    if number.startswith("4"):
        return "visa"
    if number[:2] in {"51", "52", "53", "54", "55"} or number[:1] == "2":
        return "mastercard"
    if number[:2] in {"34", "37"}:
        return "amex"
    if number.startswith("6"):
        return "discover"
    return "unknown"
