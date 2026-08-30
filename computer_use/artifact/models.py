"""The capability artifact: design notes section 2, as pydantic models.

This is the deliverable of a discovery run and the input to every replay.
It is shaped to read as a *function signature* -- named, typed inputs and
outputs -- rather than as a list of clicks, because the caller is another
program deciding whether this capability does what it needs.

The validators matter as much as the fields. An artifact that references a
step that does not exist, or an input nobody declared, is broken in a way
that will not surface until replay reaches that step -- possibly minutes
in, possibly after an irreversible action. Everything checkable is checked
when the artifact loads.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from computer_use.artifact.templating import placeholders
from computer_use.surface.models import ActionType, Locator, RiskLevel

_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class SurfaceType(str, Enum):
    """Which kind of surface this capability was recorded against.

    Only `web` is implemented. The other two are named because the
    artifact schema is meant to survive an adapter swap (design notes
    section 4) -- a field that appears later is a schema migration, a
    field that is there from the start is just a value.
    """

    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class Target(BaseModel):
    """The application a capability runs against."""

    model_config = ConfigDict(extra="forbid")

    app_id: str
    surface_type: SurfaceType = SurfaceType.WEB
    base_url: str


class ValueType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputSpec(BaseModel):
    """One argument the capability takes."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ValueType = ValueType.STRING
    required: bool = True

    #: Never written to the artifact or to logs in plaintext (design notes
    #: section 6). A sensitive value is therefore always a parameter: it
    #: cannot remain a literal in a step, because the artifact is the very
    #: thing it must stay out of.
    sensitive: bool = False

    #: A non-secret example, kept to make the artifact readable and to give
    #: replay something to validate shape against. Suppressed for sensitive
    #: inputs, where an example would defeat the point.
    example: str | None = None

    @model_validator(mode="after")
    def _sensitive_inputs_carry_no_example(self) -> InputSpec:
        if self.sensitive and self.example is not None:
            raise ValueError(f"input {self.name!r} is sensitive and cannot carry an example")
        return self


class OutputSpec(BaseModel):
    """One value the capability produces, and the step that produced it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ValueType = ValueType.STRING
    source_step: str


class CheckpointType(str, Enum):
    URL_CONTAINS = "url_contains"
    TEXT_PRESENT = "text_present"
    ELEMENT_VISIBLE = "element_visible"


class Checkpoint(BaseModel):
    """A post-condition asserted after a step, before advancing."""

    model_config = ConfigDict(extra="forbid")

    type: CheckpointType
    value: str


class MatchType(str, Enum):
    TEXT = "text"
    URL = "url"


class OutcomeMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: MatchType
    value: str


class BusinessOutcome(BaseModel):
    """Something that can legitimately go wrong at this step.

    Declared per step rather than per capability because "what can go
    wrong here" is a property of the step: a login can be rejected, a
    checkout cannot. Matching one of these makes the run a *result*, not
    an error -- the distinction design notes section 3 calls the single
    most important choice in the system.
    """

    model_config = ConfigDict(extra="forbid")

    match: OutcomeMatch
    outcome_code: str
    detail: str | None = None


class Step(BaseModel):
    """One action in the compiled flow."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    action: ActionType
    locator: Locator | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    checkpoint: Checkpoint | None = None

    #: Graded per step, not per capability: a flow routinely mixes a dozen
    #: harmless reads with one press that commits (design notes section 2).
    risk_level: RiskLevel = RiskLevel.SAFE
    approved: bool = False
    timeout_ms: int = Field(default=5000, gt=0)
    expected_business_outcomes: list[BusinessOutcome] = Field(default_factory=list)

    #: Set on the step whose extracted value feeds a declared output.
    output_name: str | None = None

    def template_strings(self) -> list[str]:
        """Every string in this step that may contain a placeholder."""
        texts = [value for value in self.params.values() if isinstance(value, str)]
        if self.locator is not None:
            texts.extend(
                spec.value for spec in self.locator.tiers if isinstance(spec.value, str)
            )
        return texts


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class DiscoveredBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    goal: str | None = None


class Provenance(BaseModel):
    """Where this capability came from, and whether anyone has blessed it."""

    model_config = ConfigDict(extra="forbid")

    discovered_by: DiscoveredBy
    approval_state: ApprovalState = ApprovalState.DRAFT


class Capability(BaseModel):
    """A reusable, replayable capability. The unit this system produces."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    version: str = "1.0.0"
    name: str
    description: str
    target: Target
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint | None = None
    provenance: Provenance
    redaction_policy: str = "default_v1"

    @model_validator(mode="after")
    def _capability_id_is_an_identifier(self) -> Capability:
        if not _ID.match(self.capability_id):
            raise ValueError(
                f"capability_id {self.capability_id!r} must be snake_case; it names a "
                "file and is called by other programs"
            )
        return self

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> Capability:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate step_id {step.step_id!r}")
            seen.add(step.step_id)
        return self

    @model_validator(mode="after")
    def _outputs_reference_real_steps(self) -> Capability:
        """An output pointing at a step that does not exist is unfixable at replay."""
        step_ids = {step.step_id for step in self.steps}
        for output in self.outputs:
            if output.source_step not in step_ids:
                raise ValueError(
                    f"output {output.name!r} names source_step "
                    f"{output.source_step!r}, which is not a step in this capability"
                )
        return self

    @model_validator(mode="after")
    def _placeholders_reference_declared_inputs(self) -> Capability:
        """Catch a template referring to an argument the caller cannot supply."""
        declared = {spec.name for spec in self.inputs}
        for step in self.steps:
            for text in step.template_strings():
                unknown = placeholders(text) - declared
                if unknown:
                    missing = ", ".join(sorted(unknown))
                    raise ValueError(
                        f"step {step.step_id!r} references undeclared input(s): {missing}"
                    )
        return self

    @model_validator(mode="after")
    def _every_input_is_used(self) -> Capability:
        """A declared input nothing consumes is a lie about the contract."""
        used: set[str] = set()
        for step in self.steps:
            for text in step.template_strings():
                used |= placeholders(text)
        unused = {spec.name for spec in self.inputs} - used
        if unused:
            raise ValueError(f"declared but unused input(s): {', '.join(sorted(unused))}")
        return self

    def input(self, name: str) -> InputSpec | None:
        return next((spec for spec in self.inputs if spec.name == name), None)

    @property
    def sensitive_input_names(self) -> set[str]:
        return {spec.name for spec in self.inputs if spec.sensitive}
