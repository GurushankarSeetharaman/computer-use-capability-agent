"""The replay result contract: design notes section 3.

Three top-level statuses, not one exception hierarchy with severities.
This is the single most important shape in the system, so it is worth
being explicit about why.

    success           the capability did what it says it does
    business_outcome  the application said no, and that is an answer --
                      invalid credentials, item out of stock, limit
                      exceeded. The caller asked a question and got one of
                      its legitimate replies.
    failure           the automation broke. Nobody asked for this, nobody
                      can act on it without evidence, and a human needs to
                      look at it.

Collapsing the middle case into "error" is the mistake this design exists
to avoid. A caller that cannot distinguish "the password was wrong" from
"the automation is broken" will either retry something that will never
succeed, or page a human for a routine rejection.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


class ReplayResult(BaseModel):
    """What a replay returns. Fields are per-status; see to_contract()."""

    status: ReplayStatus
    run_id: str
    capability_id: str

    #: success
    outputs: dict[str, Any] = Field(default_factory=dict)

    #: business_outcome
    code: str | None = None
    detail: str | None = None

    #: failure
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence_path: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ReplayStatus.SUCCESS

    def to_contract(self) -> dict[str, Any]:
        """The section 3 wire shape: only the fields that status implies.

        A success carrying a null `observed`, or a failure carrying an
        empty `outputs`, invites a caller to test the wrong field. The
        contract is narrow on purpose.
        """
        if self.status is ReplayStatus.SUCCESS:
            return {"status": self.status.value, "outputs": self.outputs}
        if self.status is ReplayStatus.BUSINESS_OUTCOME:
            return {
                "status": self.status.value,
                "code": self.code,
                "detail": self.detail,
            }
        return {
            "status": self.status.value,
            "step_id": self.step_id,
            "expected": self.expected,
            "observed": self.observed,
            "evidence_path": self.evidence_path,
        }

    # -- constructors ------------------------------------------------------

    @classmethod
    def success(cls, *, run_id: str, capability_id: str, outputs: dict[str, Any]):
        return cls(
            status=ReplayStatus.SUCCESS,
            run_id=run_id,
            capability_id=capability_id,
            outputs=outputs,
        )

    @classmethod
    def business_outcome(
        cls, *, run_id: str, capability_id: str, code: str, detail: str | None = None
    ):
        return cls(
            status=ReplayStatus.BUSINESS_OUTCOME,
            run_id=run_id,
            capability_id=capability_id,
            code=code,
            detail=detail,
        )

    @classmethod
    def failure(
        cls,
        *,
        run_id: str,
        capability_id: str,
        step_id: str,
        expected: str,
        observed: str,
        evidence_path: str | None = None,
    ):
        return cls(
            status=ReplayStatus.FAILURE,
            run_id=run_id,
            capability_id=capability_id,
            step_id=step_id,
            expected=expected,
            observed=observed,
            evidence_path=evidence_path,
        )
