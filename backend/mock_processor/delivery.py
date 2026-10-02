"""Webhook delivery: signing, transports, and the messy-delivery modes."""

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings


logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Processor-Signature"

MODE_NORMAL = "normal"
MODE_DUPLICATE = "duplicate"
MODE_REVERSE = "reverse"
MODE_CONCURRENT = "concurrent"
MODES = (MODE_NORMAL, MODE_DUPLICATE, MODE_REVERSE, MODE_CONCURRENT)


def signed_payload(event: dict) -> bytes:
    """Canonical bytes for an event. Signing happens over exactly these bytes."""
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode()


def sign_event(body: bytes, secret: str | None = None) -> str:
    secret = settings.PROCESSOR_WEBHOOK_SECRET if secret is None else secret
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@dataclass
class DeliveryResult:
    event_id: str
    status_code: int
    body: str


Transport = Callable[[bytes, str], DeliveryResult]


class HttpTransport:
    """POST the signed body to the receiver over HTTP (stdlib only)."""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def __call__(self, body: bytes, signature: str) -> DeliveryResult:
        request = urllib.request.Request(  # noqa: S310 - URL comes from settings/CLI
            self.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
        )
        event_id = json.loads(body).get("event_id", "?")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return DeliveryResult(event_id, response.status, response.read().decode())
        except urllib.error.HTTPError as exc:
            return DeliveryResult(event_id, exc.code, exc.read().decode())


class DjangoClientTransport:
    """In-process delivery through Django's test client (used by the test-suite)."""

    def __init__(self, path: str = "/webhooks/processor"):
        from django.test import Client

        self.path = path
        self._client_factory = Client

    def __call__(self, body: bytes, signature: str) -> DeliveryResult:
        client = self._client_factory()
        response = client.post(
            self.path,
            data=body,
            content_type="application/json",
            headers={SIGNATURE_HEADER: signature},
        )
        event_id = json.loads(body).get("event_id", "?")
        return DeliveryResult(event_id, response.status_code, response.content.decode())


def deliver_one(event: dict, transport: Transport) -> DeliveryResult:
    body = signed_payload(event)
    result = transport(body, sign_event(body))
    logger.info(
        "delivered %s status=%s -> HTTP %s", event["event_id"], event["status"], result.status_code
    )
    return result


def deliver(
    events: list[dict],
    mode: str,
    transport: Transport,
    sleep: Callable[[float], None] = time.sleep,
    settle_after_seconds: int = 0,
) -> list[DeliveryResult]:
    """Deliver ``[pending, final]`` according to a messy-delivery mode.

    normal:     pending, then final.
    duplicate:  every event delivered twice, back to back.
    reverse:    final first, then pending.
    concurrent: pending, then two copies of final released at the same instant
                from two threads (a barrier guarantees they are in flight together).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    pending, final = events
    results: list[DeliveryResult] = []

    def wait_for_settlement():
        if settle_after_seconds:
            logger.info("charge settles in %ss; waiting", settle_after_seconds)
            sleep(settle_after_seconds)

    if mode == MODE_NORMAL:
        results.append(deliver_one(pending, transport))
        wait_for_settlement()
        results.append(deliver_one(final, transport))
    elif mode == MODE_DUPLICATE:
        results.append(deliver_one(pending, transport))
        results.append(deliver_one(pending, transport))
        wait_for_settlement()
        results.append(deliver_one(final, transport))
        results.append(deliver_one(final, transport))
    elif mode == MODE_REVERSE:
        wait_for_settlement()
        results.append(deliver_one(final, transport))
        results.append(deliver_one(pending, transport))
    elif mode == MODE_CONCURRENT:
        results.append(deliver_one(pending, transport))
        wait_for_settlement()
        results.extend(deliver_concurrently([final, final], transport))
    return results


def deliver_concurrently(events: list[dict], transport: Transport) -> list[DeliveryResult]:
    """Fire every event from its own thread, released together by a barrier."""
    barrier = threading.Barrier(len(events))
    results: list[DeliveryResult | None] = [None] * len(events)
    errors: list[BaseException] = []

    def worker(index: int, event: dict):
        from django.db import connection

        try:
            barrier.wait()
            results[index] = deliver_one(event, transport)
        except BaseException as exc:  # noqa: BLE001 - surfaced to caller below
            errors.append(exc)
        finally:
            # Each thread owns its own DB connection (relevant for in-process transports).
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(i, event), name=f"webhook-{i}")
        for i, event in enumerate(events)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return [r for r in results if r is not None]
