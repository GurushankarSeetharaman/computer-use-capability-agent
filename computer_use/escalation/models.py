"""Who is driving, and what a human needs in order to take over.

The load-bearing idea in design notes section 5 is that control transfer
is a *state change on a live session*, not a restart. That makes
`control_owner` the most important field in this package: it is the
system's answer to "who is in control right now?", and it has to be
answerable at any instant, from either side of the handoff.

It is therefore guarded by a lock rather than being a plain attribute. The
operator console runs on a background thread, so the flip genuinely races
the automation loop: without the lock, a resume arriving mid-step could be
read as half-applied.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ControlOwner(str, Enum):
    """Exactly one of these is true at any moment."""

    AUTOMATION = "automation"
    HUMAN = "human"


class EscalationOutcome(str, Enum):
    """How a handoff ended."""

    RESUMED = "resumed"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"


class InterventionRequest(BaseModel):
    """What a human needs to take over, without digging through logs.

    Every field earns its place by answering a question the person picking
    this up will otherwise have to ask: which run, what was it trying to
    do, where did it stop, what did it look like, and why did it stop.
    """

    run_id: str
    capability_or_goal: str
    current_step: int | str
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


class RunState:
    """Mutable control state for one run, safe to read from either thread.

    Deliberately not a pydantic model: it is shared mutable state with a
    lock, and a validating value object would invite copies -- of which
    there must be exactly none, since the whole point is that both the
    automation and the console are looking at the same one.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._lock = threading.Lock()
        self._owner = ControlOwner.AUTOMATION
        #: Set when a human hands control back; the loop waits on it.
        self.resumed = threading.Event()

    @property
    def control_owner(self) -> ControlOwner:
        with self._lock:
            return self._owner

    @property
    def human_in_control(self) -> bool:
        return self.control_owner is ControlOwner.HUMAN

    def hand_to_human(self) -> ControlOwner:
        """Flip to human control. Idempotent."""
        with self._lock:
            self._owner = ControlOwner.HUMAN
            self.resumed.clear()
            return self._owner

    def hand_to_automation(self) -> ControlOwner:
        """Flip back. Wakes whatever is waiting on the handoff."""
        with self._lock:
            self._owner = ControlOwner.AUTOMATION
        self.resumed.set()
        return ControlOwner.AUTOMATION

    def snapshot(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "control_owner": self.control_owner.value}
