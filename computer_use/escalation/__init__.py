"""Escalation: pause, hand control to a human, resume.

Triggered by a stuck discovery loop, an unclassified replay failure, or an
irreversible step lacking approval (design notes section 5).

Why the Playwright context is never closed on handoff: "escalation" that
tears down the session and asks a human to start over is a restart, not a
handoff. The load-bearing property is that the human drives the *same*
live page the automation was driving, then hands it back mid-flow. A
single ``control_owner`` field (automation | human), flipped atomically,
is the system's answer to "who is in control right now?".

Resuming continues at the *next* step rather than retrying the one that
escalated. That matters most for the irreversible case: if a human took
over to place an order themselves, re-attempting the step they just
performed would place it twice.
"""

from computer_use.escalation.console import OperatorConsole
from computer_use.escalation.manager import (
    DEFAULT_TIMEOUT_S,
    EscalationManager,
    request_intervention,
)
from computer_use.escalation.models import (
    ControlOwner,
    EscalationOutcome,
    InterventionRequest,
    RunState,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ControlOwner",
    "EscalationManager",
    "EscalationOutcome",
    "InterventionRequest",
    "OperatorConsole",
    "RunState",
    "request_intervention",
]
