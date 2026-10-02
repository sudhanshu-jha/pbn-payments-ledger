"""Defence in depth: scrub anything that looks like a PAN/account number or a
CVV from log records before any handler formats them. Code must never log
these values in the first place (a test proves it); this filter catches a
future mistake.
"""

import logging
import re


LONG_DIGIT_RUN = re.compile(r"(?<![\d-])\d(?:[ -]?\d){8,18}(?![\d-])")
CVV_FIELD = re.compile(r"(?i)(\b(?:cvv|cvc|cvv2|security_code)\b\W{0,5})(\d{3,4})")
REDACTED = "[REDACTED]"


def scrub(text: str) -> str:
    text = CVV_FIELD.sub(lambda m: m.group(1) + REDACTED, text)
    return LONG_DIGIT_RUN.sub(REDACTED, text)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _scrub_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_scrub_value(v) for v in record.args)
        return True


def _scrub_value(value):
    return scrub(value) if isinstance(value, str) else value
