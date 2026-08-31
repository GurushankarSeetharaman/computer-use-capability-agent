"""Deterministic replay of a capability artifact. No model in the loop.

The expensive, non-deterministic reasoning happened once, at discovery.
This is an interpreter walking a JSON document: the same artifact and the
same inputs produce the same actions, in the same order, every time.

Nothing here imports the Anthropic SDK, and that is load-bearing rather
than incidental. The moment replay can ask a model what to do, every
property this system sells -- auditability, repeatability, cost -- becomes
conditional on what the model happened to say that day.

Policy is enforced the same way discovery enforces it: through
surface.act(), which checks the guardrail internally. Replay does not get
its own check, because two checks eventually disagree and the interesting
failure is the path that forgot to call one (design notes section 6).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from computer_use.artifact.models import (
    Capability,
    Checkpoint,
    CheckpointType,
    MatchType,
    Step,
    ValueType,
)
from computer_use.artifact.templating import render
from computer_use.escalation.models import EscalationOutcome, InterventionRequest
from computer_use.evidence.log import EvidenceLog
from computer_use.replay.models import ReplayResult
from computer_use.replay.recovery import BUILT_IN_PATTERNS, RecoveryPattern, find_pattern
from computer_use.surface.models import (
    Action,
    ActionResult,
    ActionType,
    Locator,
    SurfaceSnapshot,
    WaitCondition,
)

_PYTHON_TYPES: dict[ValueType, type] = {
    ValueType.STRING: str,
    ValueType.INTEGER: int,
    ValueType.NUMBER: float,
    ValueType.BOOLEAN: bool,
}


class SurfaceLike(Protocol):
    def perceive(self, *, screenshot: bool = True) -> SurfaceSnapshot: ...

    def act(self, action: Action) -> ActionResult: ...


class InputValidationError(ValueError):
    """The caller's arguments do not satisfy the capability's contract."""


def input_model(capability: Capability) -> type[BaseModel]:
    """Build a pydantic model from the artifact's declared inputs.

    Generated rather than hand-written so the artifact stays the single
    source of truth: a capability that grows an input does not need a
    matching change here to be validated. Unknown arguments are rejected --
    a caller passing `user_name` when the contract says `username` has made
    a mistake worth hearing about immediately, not one to discover as an
    unrendered placeholder several steps in.
    """
    fields: dict[str, Any] = {}
    for spec in capability.inputs:
        annotation = _PYTHON_TYPES[spec.type]
        if spec.required:
            fields[spec.name] = (annotation, ...)
        else:
            fields[spec.name] = (annotation | None, None)
    return create_model(
        f"{capability.capability_id}_inputs",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def validate_inputs(capability: Capability, values: dict[str, Any]) -> dict[str, Any]:
    try:
        return input_model(capability)(**values).model_dump()
    except ValidationError as exc:
        raise InputValidationError(str(exc)) from exc


class ReplayEngine:
    """Executes one capability against one surface."""

    def __init__(
        self,
        surface: SurfaceLike,
        *,
        evidence_root: str = "evidence",
        max_retries: int = 3,
        backoff_s: float = 0.5,
        patterns: tuple[RecoveryPattern, ...] = BUILT_IN_PATTERNS,
        escalation: Any | None = None,
        force_escalate_at_step: str | None = None,
        verbose: bool = False,
    ) -> None:
        #: Screenshot every step rather than only failures and handoffs.
        self.verbose = verbose
        self.surface = surface
        self.evidence_root = evidence_root
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.patterns = patterns
        #: Trigger (b) of design notes section 5: an unclassified failure
        #: hands off to a human rather than simply ending the run.
        self.escalation = escalation
        #: Demonstration hook: force a handoff at a named step so the
        #: mechanism can be shown without waiting for something to break.
        self.force_escalate_at_step = force_escalate_at_step

    # -- entry point -------------------------------------------------------

    def run(
        self,
        capability: Capability,
        values: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> ReplayResult:
        """Replay a capability, returning one of the three section 3 statuses."""
        resolved = validate_inputs(capability, values)
        run_id = run_id or f"replay_{uuid.uuid4().hex[:12]}"

        secrets = {
            str(resolved[name])
            for name in capability.sensitive_input_names
            if resolved.get(name) is not None
        }
        log = EvidenceLog(run_id, root=self.evidence_root, secrets=secrets)
        # The run owns the run_id, so it is the only thing that can point
        # the surface at the right screenshot folder.
        if hasattr(self.surface, "screenshot_dir"):
            self.surface.screenshot_dir = log.screenshots
        log.write(
            "replay_started",
            capability_id=capability.capability_id,
            version=capability.version,
            inputs=sorted(resolved),
        )

        outputs: dict[str, Any] = {}
        for step in capability.steps:
            outcome = self._run_step(step, resolved, capability, log, run_id, outputs)
            if outcome is not None:
                log.write("replay_finished", status=outcome.status.value)
                return outcome

        final = self._check_success(capability, log, run_id)
        if final is not None:
            log.write("replay_finished", status=final.status.value)
            return final

        log.write("replay_finished", status="success", outputs=outputs)
        return ReplayResult.success(
            run_id=run_id, capability_id=capability.capability_id, outputs=outputs
        )

    # -- one step ----------------------------------------------------------

    def _run_step(
        self,
        step: Step,
        values: dict[str, Any],
        capability: Capability,
        log: EvidenceLog,
        run_id: str,
        outputs: dict[str, Any],
    ) -> ReplayResult | None:
        """Execute one step and classify what happened. None means continue."""
        action = self._build_action(step, values)

        if self.force_escalate_at_step == step.step_id:
            self._escalate(
                capability, log, run_id, step.step_id, "forced escalation (demonstration)"
            )

        for attempt in range(self.max_retries + 1):
            result = self.surface.act(action)
            snapshot = self.surface.perceive(screenshot=self.verbose)
            passed, observed = self._evaluate(step.checkpoint, snapshot)
            ok = result.succeeded and passed

            log.write(
                "step",
                step_id=step.step_id,
                action=step.action.value,
                attempt=attempt,
                actor="automation",
                succeeded=result.succeeded,
                blocked=result.blocked,
                # The drift signal of section 4, in the log rather than on
                # a terminal that nobody keeps.
                locator_tier=result.locator_tier_label,
                locator_strategy=(
                    result.locator_strategy.value if result.locator_strategy else None
                ),
                locator_attempts=[a.model_dump(mode="json") for a in result.locator_attempts],
                checkpoint=step.checkpoint.model_dump(mode="json") if step.checkpoint else None,
                checkpoint_passed=passed,
                observed=observed,
                url=snapshot.url,
                error=result.error,
            )

            if ok:
                if step.output_name and result.extracted is not None:
                    outputs[step.output_name] = result.extracted
                return None

            # Order matters and is the whole classification: a legitimate
            # answer first, then a hiccup worth retrying, then a defect.
            outcome = self._match_business_outcome(step, snapshot)
            if outcome is not None:
                log.write(
                    "business_outcome", step_id=step.step_id, code=outcome.outcome_code
                )
                return ReplayResult.business_outcome(
                    run_id=run_id,
                    capability_id=capability.capability_id,
                    code=outcome.outcome_code,
                    detail=outcome.detail or observed,
                )

            if result.blocked:
                # Policy refusals are never retried: the answer will not
                # change, and an irreversible step is waiting on a human.
                break

            pattern = find_pattern(snapshot, self.patterns)
            if pattern is not None and attempt < self.max_retries:
                log.write(
                    "recovering",
                    step_id=step.step_id,
                    pattern=pattern.name,
                    attempt=attempt,
                )
                remedy = pattern.remedy(snapshot)
                if remedy is not None:
                    self.surface.act(remedy)
                # Spacing retries, not waiting for a condition -- waiting on
                # a condition is what wait_for steps are for.
                time.sleep(self.backoff_s * (attempt + 1))
                continue

            break

        expected = self._describe_expectation(step)
        # Always screenshot a failure, whatever --verbose says: this is the
        # frame someone will actually want, and it is the one moment where
        # not having it costs a re-run.
        failure_snapshot = self.surface.perceive(screenshot=True)
        _, observed = self._evaluate(step.checkpoint, failure_snapshot)
        log.write(
            "failure",
            step_id=step.step_id,
            expected=expected,
            observed=observed,
            screenshot=failure_snapshot.screenshot_path,
        )

        # Trigger (b): unclassified failure. If a human takes the session
        # and hands it back, replay continues at the NEXT step -- never by
        # retrying the one that escalated, which for an irreversible step a
        # human just performed by hand would do it twice.
        if self.escalation is not None:
            outcome = self._escalate(capability, log, run_id, step.step_id, expected)
            if outcome is EscalationOutcome.RESUMED:
                log.write("resumed", step_id=step.step_id, continuing_at="next_step")
                return None

        return ReplayResult.failure(
            run_id=run_id,
            capability_id=capability.capability_id,
            step_id=step.step_id,
            expected=expected,
            observed=observed,
            evidence_path=str(log.path),
        )

    def _escalate(
        self, capability: Capability, log: EvidenceLog, run_id: str, step_id: str, reason: str
    ) -> Any:
        """Hand the live session to a human. The surface is never closed."""
        snapshot = self.surface.perceive(screenshot=True)
        request = InterventionRequest(
            run_id=run_id,
            capability_or_goal=capability.capability_id,
            current_step=step_id,
            reason=reason,
            url=snapshot.url,
            screenshot_path=snapshot.screenshot_path,
        )
        return self.escalation.pause(request, surface=self.surface)

    # -- building ----------------------------------------------------------

    def _build_action(self, step: Step, values: dict[str, Any]) -> Action:
        params = {
            key: render(value, values) if isinstance(value, str) else value
            for key, value in step.params.items()
        }
        condition = params.get("condition")
        return Action(
            type=step.action,
            locator=self._render_locator(step.locator, values),
            url=params.get("url"),
            value=params.get("value"),
            condition=WaitCondition(condition) if condition else None,
            timeout_ms=step.timeout_ms,
            risk_level=step.risk_level,
            approved=step.approved,
        )

    @staticmethod
    def _render_locator(locator: Locator | None, values: dict[str, Any]) -> Locator | None:
        """Substitute inputs into a locator's tiers.

        Locators are templated for the product-id case described in the
        compiler; everything else passes through unchanged.
        """
        if locator is None:
            return None

        def substitute(spec: dict[str, Any]) -> dict[str, Any]:
            return {
                key: render(value, values) if isinstance(value, str) else value
                for key, value in spec.items()
            }

        return Locator(
            strategy=locator.strategy,
            primary=substitute(locator.primary),
            fallbacks=[substitute(fallback) for fallback in locator.fallbacks],
        )

    # -- evaluation --------------------------------------------------------

    @staticmethod
    def _evaluate(
        checkpoint: Checkpoint | None, snapshot: SurfaceSnapshot
    ) -> tuple[bool, str]:
        """Assert a post-condition against what is on screen.

        Evaluated against a snapshot rather than the live page so this
        stays adapter-agnostic -- and testable without a browser.
        """
        if checkpoint is None:
            return True, snapshot.url
        if checkpoint.type is CheckpointType.URL_CONTAINS:
            return checkpoint.value in snapshot.url, snapshot.url
        text = _visible_text(snapshot)
        if checkpoint.type is CheckpointType.TEXT_PRESENT:
            return checkpoint.value in text, _summarise(text)
        if checkpoint.type is CheckpointType.ELEMENT_VISIBLE:
            present = any(node.name == checkpoint.value for node in snapshot.nodes)
            return present, _summarise(text)
        return False, _summarise(text)

    @staticmethod
    def _match_business_outcome(step: Step, snapshot: SurfaceSnapshot):
        """Is this one of the answers the application is allowed to give?"""
        text = _visible_text(snapshot)
        for outcome in step.expected_business_outcomes:
            if outcome.match.type is MatchType.TEXT and outcome.match.value in text:
                return outcome
            if outcome.match.type is MatchType.URL and outcome.match.value in snapshot.url:
                return outcome
        return None

    def _check_success(
        self, capability: Capability, log: EvidenceLog, run_id: str
    ) -> ReplayResult | None:
        """The capability-level post-condition, after the last step."""
        if capability.success_checkpoint is None:
            return None
        snapshot = self.surface.perceive(screenshot=True)
        passed, observed = self._evaluate(capability.success_checkpoint, snapshot)
        if passed:
            return None
        expected = (
            f"{capability.success_checkpoint.type.value}="
            f"{capability.success_checkpoint.value!r}"
        )
        log.write("failure", step_id="success_checkpoint", expected=expected, observed=observed)
        return ReplayResult.failure(
            run_id=run_id,
            capability_id=capability.capability_id,
            step_id="success_checkpoint",
            expected=expected,
            observed=observed,
            evidence_path=str(log.path),
        )

    @staticmethod
    def _describe_expectation(step: Step) -> str:
        if step.checkpoint is None:
            return f"{step.action.value} to succeed"
        return f"{step.checkpoint.type.value}={step.checkpoint.value!r}"


def _visible_text(snapshot: SurfaceSnapshot) -> str:
    return " ".join(
        part for node in snapshot.nodes for part in (node.name, node.value) if part
    )


def _summarise(text: str, limit: int = 240) -> str:
    return text if len(text) <= limit else text[:limit] + "..."
