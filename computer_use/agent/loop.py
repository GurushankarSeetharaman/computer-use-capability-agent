"""The discovery loop: observe, decide, act, record, repeat.

    observe   surface.perceive()
    decide    Claude picks exactly one tool call, given the goal, the
              current snapshot, and everything that happened so far
    act       surface.act(), which enforces the guardrail internally
    record    the whole tuple, successes and mistakes alike

The loop does not call the guardrail itself. It cannot: enforcement lives
inside act(), so there is no path from a model's decision to the page that
skips policy. What the loop does instead is *react* to a refusal -- feeding
it back to Claude as a tool result, so the model learns the boundary
rather than repeating the attempt.

Stopping is treated as a first-class outcome. Running out of steps, timing
out, and the model reporting itself stuck are all recorded as what they
are, because a discovery run that quietly produced a half-flow would
compile into a capability that half-works.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from computer_use.agent.recording import (
    DiscoveryOutcome,
    RecordedStep,
    Recording,
    StepOutcome,
)
from computer_use.agent.tools import (
    CONTROL_TOOLS,
    TOOL_SCHEMAS,
    Decision,
    interpret,
)
from computer_use.evidence.log import EvidenceLog
from computer_use.evidence.redaction import looks_sensitive
from computer_use.escalation import (
    EscalationOutcome,
    InterventionRequest,
    request_intervention,
)
from computer_use.surface.models import ActionResult, SurfaceSnapshot, TierOutcome
from computer_use.templating import placeholders, render_partial

#: Named by the operator, not guessed. Sonnet 5 is capable enough to read an
#: accessibility tree and pick the next action, and discovery is the only
#: place in this system that spends model tokens at all.
DEFAULT_MODEL = "claude-sonnet-5"

#: Generous enough that adaptive thinking plus one tool call never truncates
#: mid-decision, which would look like a malformed tool call rather than the
#: budget problem it is.
DEFAULT_MAX_TOKENS = 8192

SYSTEM_PROMPT = """You are driving a real web browser to accomplish a goal, one action per turn.

How to work:
- You are shown a pruned accessibility tree after every action. Address elements by the
  accessible `role` and `name` exactly as they appear there. Do not invent names.
- Call exactly ONE tool per turn. After each action you will see the resulting page.
- Prefer clicking links over navigating to URLs you guessed.
- After an action that changes the page, use `wait_for` before assuming the new page is
  loaded.
- Use `extract` for every value the goal asks you to report, giving each a snake_case
  `output_name`. These become the named outputs of the reusable capability compiled from
  this run, so they are the point of the exercise, not bookkeeping.
- Plain page content appears in the snapshot as `text content: "..."`. It has no
  accessible name, so read it with `extract` using the `text` field, not role+name. Give
  the stable label part ('Total:'), never the value you expect to read ('Total: $32.39') --
  a locator containing today's value will not match tomorrow's.
- A locator must identify exactly ONE element. If an action is refused as ambiguous, that
  is not a dead end: narrow it and try again. For text, set `text_regex` and anchor the
  label ('^Total: '). For a control, add a `css_fallback`. Do not give up on a value the
  goal asked for just because the first locator was too broad.
- Credentials are supplied to the run as named parameters, and you are never shown their
  values. To fill a credential field, pass the placeholder as the value -- `${username}`,
  `${password}` -- exactly as written. The runner substitutes the real value at the moment
  it types, so the secret is never in this conversation. Never invent, guess, or repeat a
  credential value, and never put one in a summary.
- Set `risk_level` honestly on every action. Anything that submits, pays, commits or
  deletes is `irreversible` and will stop for human approval.

This run is being recorded and compiled into a capability that will later be replayed
without you. Prefer the stable, obvious path over a clever shortcut.

If an action is refused by policy, do not retry it or work around it. Either choose a
different approach that stays in bounds, or call `report_stuck`.

Call `done` when the goal is met, with the values you extracted. Call `report_stuck` if
you cannot proceed without guessing. Stopping honestly is a correct outcome."""


class SurfaceLike(Protocol):
    """The surface capability the loop needs -- perceive and act, nothing more."""

    def perceive(self, *, screenshot: bool = True) -> SurfaceSnapshot: ...

    def act(self, action: Any) -> ActionResult: ...


class MessagesClient(Protocol):
    """The slice of the Anthropic client used here, so tests can substitute one."""

    def create(self, **kwargs: Any) -> Any: ...


@dataclass
class DiscoveryResult:
    """The outcome of a run, plus the raw recording that produced it."""

    recording: Recording
    outcome: DiscoveryOutcome
    summary: str | None = None
    outputs: dict[str, str] | None = None
    intervention: InterventionRequest | None = None
    evidence_dir: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is DiscoveryOutcome.GOAL_MET


class DiscoveryAgent:
    """Runs one goal to completion, or to an honest stop.

    The Anthropic client is injected rather than constructed here so the
    loop's control flow -- stop conditions, refusal handling, recording --
    can be tested exhaustively against scripted responses, with no API key
    and no network.
    """

    def __init__(
        self,
        surface: SurfaceLike,
        *,
        client: Any,
        model: str = DEFAULT_MODEL,
        max_steps: int = 25,
        timeout_s: float = 300.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        escalation: Any | None = None,
        evidence_root: str = "evidence",
        verbose: bool = False,
    ) -> None:
        self.evidence_root = evidence_root
        self._credentials: dict[str, str] = {}
        #: Screenshot every step rather than only failures and handoffs.
        #: Off by default: a screenshot per step on a 25-step run is a lot
        #: of disk for frames nobody looks at, and the ones that matter are
        #: captured either way.
        self.verbose = verbose
        #: Trigger (a) of design notes section 5. With a manager attached,
        #: report_stuck hands the live session to a human instead of ending
        #: the run; without one it stays a terminal outcome.
        self.escalation = escalation
        self.surface = surface
        self.client = client
        self.model = model
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    # -- entry point -------------------------------------------------------

    def run(
        self,
        goal: str,
        target: str,
        *,
        credentials: dict[str, str] | None = None,
        run_id: str | None = None,
    ) -> DiscoveryResult:
        """Pursue one goal, then persist the run's evidence whatever happened.

        Credentials are passed here as structured parameters rather than
        written into the goal text. Two things follow, and both matter.

        The redactor learns them *before the first line is written*, so a
        credential cannot reach the log ahead of the step that uses it --
        which is exactly how one used to escape when the goal string
        quoted it.

        And the model is never shown the values at all. It refers to them
        by placeholder; substitution happens at the moment of typing. A
        secret that never enters the conversation cannot be echoed back in
        a summary, leaked through a tool argument, or retained in a
        transcript sent to an API.
        """
        recording = Recording(
            run_id=run_id or f"run_{uuid.uuid4().hex[:12]}",
            goal=goal,
            target=target,
            model=self.model,
        )
        self._credentials = dict(credentials or {})
        # Seeded before the first write. Ordering is the whole fix.
        log = EvidenceLog(
            recording.run_id,
            root=self.evidence_root,
            secrets=set(self._credentials.values()),
        )
        # The run owns the run_id, so it is the only thing that can point
        # the surface at the right screenshot folder.
        if hasattr(self.surface, "screenshot_dir"):
            self.surface.screenshot_dir = log.screenshots
        log.write("discovery_started", goal=goal, target=target, model=self.model)
        self._log = log

        try:
            result = self._drive(recording, goal, target, log)
        finally:
            self._persist(recording, log)
        result.evidence_dir = str(log.directory)
        return result

    def _persist(self, recording: Recording, log: EvidenceLog) -> None:
        """Write the raw transcript, redacted, next to the run's log.

        Section 6 says a sensitive value is never persisted in plaintext,
        and the recording is persisted. Redaction happens on the way out
        rather than as the steps are recorded: the in-memory transcript
        keeps the literal so the compiler can still recognise it, while
        nothing with the secret in it ever reaches a disk.
        """
        raw = recording.model_dump_json(indent=2)
        (log.directory / "recording.json").write_text(log.redact(raw), encoding="utf-8")
        log.write("discovery_finished", outcome=recording.outcome.value if recording.outcome else None)

    def _drive(
        self, recording: Recording, goal: str, target: str, log: EvidenceLog
    ) -> DiscoveryResult:
        """The observe/decide/act loop itself."""
        started = time.monotonic()
        snapshot = self.surface.perceive(screenshot=True)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._opening_prompt(goal, target, snapshot)}
        ]

        while True:
            if len(recording.steps) >= self.max_steps:
                return self._stop(recording, DiscoveryOutcome.MAX_STEPS)
            if time.monotonic() - started > self.timeout_s:
                return self._stop(recording, DiscoveryOutcome.TIMEOUT)

            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                thinking={"type": "adaptive"},
                messages=messages,
            )
            # Echoed back verbatim: thinking blocks must survive unchanged
            # across turns on the same model, and dropping them would also
            # lose the tool_use ids the next message has to answer.
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                terminal = self._handle_no_tool_call(recording, response, messages)
                if terminal is not None:
                    return terminal
                continue

            terminal, tool_results = self._run_turn(recording, tool_uses, snapshot)
            if terminal is not None:
                return terminal

            # One perception per turn, shared by the model's tool result and
            # the next turn's before-state. Perceiving twice would double the
            # screenshots in the evidence trail and cost a round trip for a
            # view of the page we already have.
            snapshot = self.surface.perceive(screenshot=self.verbose)
            tool_results[0]["content"] += f"\n\nCurrent page:\n{snapshot.describe()}"
            messages.append({"role": "user", "content": tool_results})

    # -- one turn ----------------------------------------------------------

    def _run_turn(
        self, recording: Recording, tool_uses: list[Any], snapshot: SurfaceSnapshot
    ) -> tuple[DiscoveryResult | None, list[dict[str, Any]]]:
        """Execute a turn's tool calls, returning a result only if it ended the run.

        Only the first tool call is executed: the loop is defined as one
        action per turn, so that every recorded step has an unambiguous
        before-state. The rest are still answered, because the API requires
        a tool_result for every tool_use block -- an unanswered one is a
        hard 400 on the next request, and the failure mode is worse than
        the wasted call.
        """
        results: list[dict[str, Any]] = []
        terminal: DiscoveryResult | None = None

        for position, block in enumerate(tool_uses):
            if position > 0:
                results.append(
                    self._tool_result(
                        block.id,
                        "Not executed: this loop takes exactly one action per turn. "
                        "Reissue this call on its own next turn if you still want it.",
                        is_error=True,
                    )
                )
                continue

            outcome, payload_text = self._execute(recording, block, snapshot)
            results.append(self._tool_result(block.id, payload_text, is_error=outcome is None))
            if isinstance(outcome, DiscoveryResult):
                terminal = outcome

        return terminal, results

    def _execute(
        self, recording: Recording, block: Any, snapshot: SurfaceSnapshot
    ) -> tuple[DiscoveryResult | bool | None, str]:
        """Interpret and carry out one tool call, recording the step.

        Returns (terminal-or-flag, text for the model). ``None`` in the first
        slot marks an error the model should see as one.
        """
        index = recording.next_index()
        payload = dict(block.input or {})

        try:
            decision = interpret(block.name, payload)
        except ValueError as exc:
            recording.append(
                RecordedStep(
                    index=index,
                    outcome=StepOutcome.INVALID_TOOL_CALL,
                    snapshot=snapshot,
                    tool_name=block.name,
                    tool_payload=payload,
                    error=str(exc),
                )
            )
            return None, f"That tool call was rejected: {exc}. Correct it and try again."

        if decision.tool_name in CONTROL_TOOLS:
            outcome = self._handle_control(recording, decision, index, snapshot)
            # True rather than None when a handoff resumed: None means "tell
            # the model this was an error", and being handed back control is
            # not an error.
            return (outcome if outcome is not None else True), "acknowledged"

        return self._perform(recording, decision, index, snapshot)

    def _perform(
        self, recording: Recording, decision: Decision, index: int, snapshot: SurfaceSnapshot
    ) -> tuple[bool | None, str]:
        """Run one browser action through act(), which enforces policy."""
        assert decision.action is not None
        # Executed with the real value, recorded with the placeholder: the
        # transcript on disk never holds the secret, and the compiler sees
        # a parameter rather than a literal it would have to recognise.
        result = self.surface.act(self._resolve_credentials(decision.action))

        if result.blocked:
            outcome = StepOutcome.BLOCKED
        elif result.succeeded:
            outcome = StepOutcome.EXECUTED
        else:
            outcome = StepOutcome.FAILED

        recording.append(
            RecordedStep(
                index=index,
                outcome=outcome,
                snapshot=snapshot,
                tool_name=decision.tool_name,
                tool_payload=decision.payload,
                action=decision.action,
                result=result,
                output_name=decision.output_name,
                error=result.error,
            )
        )
        self._log_step(index, decision, result, outcome, snapshot)
        return (result.succeeded or None), self._describe_result(decision, result)

    def _resolve_credentials(self, action):
        """Substitute credential values into an action, just before acting."""
        if not self._credentials or not isinstance(action.value, str):
            return action
        filled = render_partial(action.value, self._credentials)
        return action if filled == action.value else action.model_copy(update={"value": filled})

    def _log_step(self, index, decision, result, outcome, snapshot) -> None:
        """One evidence line per step, matching the replay engine's shape.

        A secret is learned *before* the line that used it is written: the
        model only reveals a credential by typing it, so the redactor has
        to grow mid-run rather than being fixed up afterwards.
        """
        log = getattr(self, "_log", None)
        if log is None:
            return
        field = (
            decision.action.locator.tiers[0].name
            if decision.action is not None and decision.action.locator is not None
            else None
        )
        typed = decision.action.value if decision.action else None
        # A placeholder is not a secret. Learning "${password}" as one makes
        # the redactor replace it with [REDACTED] in the recording, which
        # costs nothing in safety and everything in usefulness: the compiler
        # would see a redaction token where a parameter should be.
        if looks_sensitive(field) and typed and not placeholders(typed):
            log.learn_secret(typed)
        log.write(
            "step",
            step_index=index,
            actor="automation",
            outcome=outcome.value,
            tool=decision.tool_name,
            action=decision.action.type.value if decision.action else None,
            target=field,
            locator_tier=result.locator_tier_label,
            locator_strategy=(
                result.locator_strategy.value if result.locator_strategy else None
            ),
            locator_attempts=[a.model_dump(mode="json") for a in result.locator_attempts],
            succeeded=result.succeeded,
            blocked=result.blocked,
            url=snapshot.url if snapshot else None,
            error=result.error,
        )

    def _handle_control(
        self, recording: Recording, decision: Decision, index: int, snapshot: SurfaceSnapshot
    ) -> DiscoveryResult | None:
        """Finish the run because the model said it is finished, or stuck."""
        recording.append(
            RecordedStep(
                index=index,
                outcome=StepOutcome.CONTROL,
                snapshot=snapshot,
                tool_name=decision.tool_name,
                tool_payload=decision.payload,
            )
        )

        if decision.is_done:
            recording.summary = decision.summary
            recording.outputs = decision.outputs
            self._stop(recording, DiscoveryOutcome.GOAL_MET)
            return DiscoveryResult(
                recording=recording,
                outcome=DiscoveryOutcome.GOAL_MET,
                summary=decision.summary,
                outputs=decision.outputs,
            )

        recording.stuck_reason = decision.reason
        request, outcome = self._escalate(recording, decision.reason or "", snapshot, index)
        if outcome is EscalationOutcome.RESUMED:
            # A human took the session and handed it back. Re-perceiving
            # happens at the top of the loop, so the model reasons from
            # wherever they left the page rather than from where it stuck.
            recording.stuck_reason = None
            return None

        self._stop(recording, DiscoveryOutcome.STUCK)
        return DiscoveryResult(
            recording=recording, outcome=DiscoveryOutcome.STUCK, intervention=request
        )

    def _handle_no_tool_call(
        self, recording: Recording, response: Any, messages: list[dict[str, Any]]
    ) -> DiscoveryResult | None:
        """Nudge a turn that produced only prose, once, then give up on it.

        A model that answers in text is not driving the browser. One nudge
        covers a stray commentary turn; a second in a row means it is not
        going to act, and looping further would just spend tokens.
        """
        text = _text_of(response)
        recording.append(
            RecordedStep(
                index=recording.next_index(),
                outcome=StepOutcome.INVALID_TOOL_CALL,
                assistant_text=text,
                error="model replied without calling a tool",
            )
        )
        previous = recording.steps[-2] if len(recording.steps) >= 2 else None
        if previous is not None and previous.error == "model replied without calling a tool":
            self._stop(recording, DiscoveryOutcome.STUCK)
            return DiscoveryResult(
                recording=recording,
                outcome=DiscoveryOutcome.STUCK,
                intervention=self._escalate(
                    recording, "model stopped calling tools", None, recording.next_index()
                )[0],
            )
        messages.append(
            {
                "role": "user",
                "content": "Continue by calling exactly one tool. If you cannot proceed, "
                "call report_stuck.",
            }
        )
        return None

    # -- helpers -----------------------------------------------------------

    def _escalate(
        self,
        recording: Recording,
        reason: str,
        snapshot: SurfaceSnapshot | None,
        index: int,
    ) -> tuple[InterventionRequest, EscalationOutcome | None]:
        """Hand the live session to a human, if one can be reached."""
        request = InterventionRequest(
            run_id=recording.run_id,
            capability_or_goal=recording.goal,
            current_step=index,
            reason=reason,
            screenshot_path=snapshot.screenshot_path if snapshot else None,
            url=snapshot.url if snapshot else None,
        )
        if self.escalation is None:
            request_intervention(request)
            return request, None
        return request, self.escalation.pause(request, surface=self.surface)

    def _stop(self, recording: Recording, outcome: DiscoveryOutcome) -> DiscoveryResult:
        recording.finish(outcome)
        return DiscoveryResult(recording=recording, outcome=outcome)

    def _opening_prompt(self, goal: str, target: str, snapshot: SurfaceSnapshot) -> str:
        return (
            f"Goal: {goal}\n"
            f"Target application: {target}\n\n"
            f"Current page:\n{snapshot.describe()}"
        )

    @staticmethod
    def _describe_result(decision: Decision, result: ActionResult) -> str:
        """What the model is told about its action. The page state is appended
        by the caller, from the single perception taken for this turn."""
        if result.blocked:
            return f"REFUSED BY POLICY: {result.guardrail_reason}"
        if not result.succeeded:
            failure = f"FAILED: {result.error}"
            # An ambiguous locator is recoverable, and the model is far more
            # likely to recover if told how at the moment it happens rather
            # than only in a system prompt it read many turns ago.
            if any(a.outcome is TierOutcome.AMBIGUOUS for a in result.locator_attempts):
                failure += (
                    "\nThat locator matched several elements, so it named none of them. "
                    "Narrow it and retry: for text, set text_regex and anchor the label "
                    "(e.g. '^Total: '); for a control, add a css_fallback."
                )
            return failure
        body = f"OK ({decision.tool_name})"
        if result.extracted is not None:
            body += f"\nextracted {decision.output_name or 'value'}: {result.extracted!r}"
        return body

    @staticmethod
    def _tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block


def _text_of(response: Any) -> str:
    return "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
