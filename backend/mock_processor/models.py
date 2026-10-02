"""Processor-side storage.

These tables belong to the *external* system. The ``payments`` app must never
import this module or query these tables; it talks to the processor only
through ``mock_processor.public``.

Only ``last4`` and the pre-decided outcome are stored. Full card/account
numbers and CVVs are discarded inside the tokenize request handler.
"""

import secrets

from django.db import models


class ProcessorToken(models.Model):
    token = models.CharField(max_length=64, unique=True)
    method = models.CharField(max_length=8)  # card | bank
    last4 = models.CharField(max_length=4)
    brand_or_bank_type = models.CharField(max_length=32)
    # outcome decided at tokenization time from last4
    final_status = models.CharField(max_length=16)
    failure_code = models.CharField(max_length=32, null=True, blank=True)  # noqa: DJ001
    settle_after_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.token} ({self.method} ****{self.last4})"

    @staticmethod
    def generate_token() -> str:
        return f"tok_{secrets.token_hex(12)}"


class ProcessorCharge(models.Model):
    processor_reference = models.CharField(max_length=64, unique=True)
    token = models.ForeignKey(ProcessorToken, on_delete=models.PROTECT, related_name="charges")
    amount = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    final_status = models.CharField(max_length=16)
    failure_code = models.CharField(max_length=32, null=True, blank=True)  # noqa: DJ001
    settle_after_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.processor_reference

    @staticmethod
    def generate_reference() -> str:
        return f"pr_{secrets.token_hex(12)}"
