"""The payment state machine, as a pure function.

    pending ──► succeeded
    pending ──► failed
    terminal states are final.

No database access here: this is the single place that encodes the transition
rules, and it is unit-tested cell by cell.
"""

from dataclasses import dataclass

from .enums import TERMINAL_STATUSES, PaymentStatus, WebhookOutcome


@dataclass(frozen=True)
class Decision:
    outcome: WebhookOutcome
    new_status: PaymentStatus | None = None

    @property
    def applies(self) -> bool:
        return self.outcome == WebhookOutcome.APPLIED


def decide(current: str, incoming: str) -> Decision:
    """Decide what an incoming processor status does to a payment in ``current``.

    - pending  + pending   -> IGNORED_REDUNDANT (already pending)
    - pending  + terminal  -> APPLIED
    - terminal + pending   -> IGNORED_STALE  (late/out-of-order pending must not regress)
    - terminal + same      -> IGNORED_REDUNDANT (semantic duplicate with a new event_id)
    - terminal + other     -> CONFLICT (first terminal wins; see README)
    """
    current = PaymentStatus(current)
    incoming = PaymentStatus(incoming)

    if current in TERMINAL_STATUSES:
        if incoming == PaymentStatus.PENDING:
            return Decision(WebhookOutcome.IGNORED_STALE)
        if incoming == current:
            return Decision(WebhookOutcome.IGNORED_REDUNDANT)
        return Decision(WebhookOutcome.CONFLICT)

    # current is pending
    if incoming == PaymentStatus.PENDING:
        return Decision(WebhookOutcome.IGNORED_REDUNDANT)
    return Decision(WebhookOutcome.APPLIED, new_status=incoming)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
