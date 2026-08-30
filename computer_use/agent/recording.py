"""The raw transcript of a discovery run.

Deliberately noisy. Retries, refused actions, dead ends and the model's own
false starts all land here exactly as they happened, because this is the
evidence of what occurred -- not a description of what should have.

Distilling this into a clean capability is the compiler's job, a separate
pass over a finished recording (design notes section 1). Keeping the two
apart is what lets the question "what if discovery had false starts?" be
answered with "they are in the recording and absent from the artifact"
rather than with a cleanup heuristic tangled into the loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from computer_use.surface.models import Action, ActionResult, SurfaceSnapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StepOutcome(str, Enum):
    """What became of one turn.

    ``blocked`` is separate from ``failed`` because policy refusals are not
    attempts that went wrong -- and the compiler must never mistake one for
    a step that merely needs a retry.
    """

    EXECUTED = "executed"
    BLOCKED = "blocked"
    FAILED = "failed"
    INVALID_TOOL_CALL = "invalid_tool_call"
    CONTROL = "control"


class RecordedStep(BaseModel):
    """One (snapshot, decision, action, result) tuple, verbatim."""

    index: int
    outcome: StepOutcome
    snapshot: SurfaceSnapshot | None = None
    assistant_text: str | None = None
    tool_name: str | None = None
    tool_payload: dict[str, Any] = Field(default_factory=dict)
    action: Action | None = None
    result: ActionResult | None = None
    output_name: str | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=_now)

    @property
    def succeeded(self) -> bool:
        return self.outcome is StepOutcome.EXECUTED and bool(
            self.result and self.result.succeeded
        )


class DiscoveryOutcome(str, Enum):
    """How a discovery run ended."""

    GOAL_MET = "goal_met"
    STUCK = "stuck"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    ERROR = "error"


class Recording(BaseModel):
    """A whole discovery run: what was asked, what happened, how it ended."""

    run_id: str
    goal: str
    target: str
    model: str
    steps: list[RecordedStep] = Field(default_factory=list)
    outcome: DiscoveryOutcome | None = None
    summary: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    stuck_reason: str | None = None
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    def append(self, step: RecordedStep) -> RecordedStep:
        self.steps.append(step)
        return step

    def next_index(self) -> int:
        return len(self.steps)

    @property
    def successful_steps(self) -> list[RecordedStep]:
        """The path that actually worked -- the compiler's starting point.

        Offered as a view rather than as the stored form: the noise stays on
        disk, and dropping it is an explicit act performed downstream.
        """
        return [step for step in self.steps if step.succeeded]

    def finish(self, outcome: DiscoveryOutcome) -> Recording:
        self.outcome = outcome
        self.finished_at = _now()
        return self
