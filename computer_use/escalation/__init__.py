"""Escalation: pause, hand control to a human, resume.

Triggered by a stuck discovery loop, an unclassified replay failure, or an
irreversible step lacking approval (design notes section 5).

Why the Playwright context is never closed on handoff: "escalation" that
tears down the session and asks a human to start over is a restart, not a
handoff. The load-bearing property is that the human drives the *same*
live page the automation was driving, then hands it back mid-flow. A
single ``control_owner`` field (automation | human), flipped atomically,
is the system's answer to "who is in control right now?".

STATUS: the InterventionRequest below is real and already carries
everything a person needs to act. Control transfer -- exposing the live
page, the resume handshake, the control_owner flip -- is not built yet;
``request_intervention`` records the request and returns it. Callers are
written against the final shape, so wiring the transfer in later does not
change their code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class InterventionRequest(BaseModel):
    """What a human needs to take over, without digging through logs.

    Every field earns its place by answering a question the person picking
    this up will otherwise have to ask: which run, what was it trying to
    do, where did it stop, what did it look like, and why did it stop.
    """

    run_id: str
    capability_or_goal: str
    current_step: int
    reason: str
    screenshot_path: str | None = None
    url: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def describe(self) -> str:
        """Operator-facing summary; printed at the point of handoff."""
        return (
            f"[escalation] run={self.run_id} step={self.current_step}\n"
            f"  goal   : {self.capability_or_goal}\n"
            f"  reason : {self.reason}\n"
            f"  url    : {self.url or '-'}\n"
            f"  screen : {self.screenshot_path or '-'}"
        )


def request_intervention(request: InterventionRequest) -> InterventionRequest:
    """Record a handoff request. Stub: does not yet transfer control.

    Prints the request so a manual run is legible, and returns it so the
    caller can attach it to a result. When control transfer lands, this is
    the single place that changes.
    """
    print(request.describe())
    return request


__all__ = ["InterventionRequest", "request_intervention"]
