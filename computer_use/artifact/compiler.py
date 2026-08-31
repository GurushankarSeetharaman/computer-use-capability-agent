"""Distilling a raw recording into a capability artifact.

A separate pass over a *finished* recording, not a mode of the agent loop
(design notes section 1). The loop produces a deliberately noisy
transcript -- refusals, failures, malformed tool calls, dead ends -- and
this is the only place that decides what of it survives. Keeping the two
apart is what lets "what if discovery had false starts?" be answered with
"they are in the recording and absent from the artifact" rather than with
a cleanup heuristic tangled into the loop.

**Only the successful path is compiled.** Every step whose outcome was not
a successful execution is dropped: a retry that eventually worked
contributes its successful attempt and nothing else, and a refused action
contributes nothing at all. This is deliberate, and it is why the raw
recording is kept on disk alongside the artifact -- the artifact is the
distillation, the recording is the evidence.

**Parameter detection is a judgement call, so it is asked, not inferred.**
Whether "12345" is a postcode the caller supplies or a constant of the
flow cannot be decided by looking at it. The compiler proposes and a human
decides, with two exceptions it does not delegate: a value whose field
looks like a credential is *always* parameterized (a secret cannot be left
as a literal in the artifact it must stay out of), and the product-id
templating described below.

**Locator templating assumes saucedemo's id convention.** Saucedemo builds
add-to-cart buttons as ``#add-to-cart-sauce-labs-backpack``, so which
product a step adds is encoded in the element id rather than in any typed
value. To parameterize the item at all, the compiler substitutes into the
locator: ``#add-to-cart-${item_name|slug}``. The literal is recovered from
the snapshot the step was taken against rather than by un-slugging, so the
proposal is grounded in what was actually on the page. **This is a
saucedemo-specific naming rule and is not guaranteed to generalize** --
another app may use numeric ids, another separator, or none, and the
substitution would then produce a locator matching nothing. A real
multi-tenant system would express this as the binding overlay of design
notes section 4, not as a string rule baked into an artifact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from computer_use.agent.recording import RecordedStep, Recording
from computer_use.artifact.models import (
    BusinessOutcome,
    Capability,
    Checkpoint,
    CheckpointType,
    DiscoveredBy,
    InputSpec,
    MatchType,
    OutcomeMatch,
    OutputSpec,
    Provenance,
    Step,
    SurfaceType,
    Target,
)
from computer_use.artifact.templating import placeholders, slugify
from computer_use.surface.models import ActionType, Locator, SurfaceSnapshot

#: Field names whose values are treated as secrets without asking. Matched
#: as substrings against the accessible name of the field being filled.
SENSITIVE_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api key",
    "apikey",
    "credential",
    "pin",
    "ssn",
    "social security",
    "card number",
    "cvv",
)

#: A slug has to be at least this long before it is offered as a
#: parameter, so that a locator ending in something incidental is not
#: proposed as the thing the capability is about.
MIN_SLUG_LENGTH = 6


class Prompter(Protocol):
    """How the compiler asks a human to make the calls it should not make."""

    def confirm(self, question: str, *, default: bool = False) -> bool: ...

    def ask(self, question: str, *, default: str = "") -> str: ...


class CliPrompter:
    """Reads answers from the terminal."""

    def confirm(self, question: str, *, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        answer = input(f"{question} {suffix} ").strip().lower()
        if not answer:
            return default
        return answer.startswith("y")

    def ask(self, question: str, *, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        return input(f"{question}{shown} ").strip() or default


class NonInteractivePrompter:
    """Takes every default without asking.

    For test runs and for recompiling an artifact in CI, where blocking on
    stdin would hang rather than fail.
    """

    def confirm(self, question: str, *, default: bool = False) -> bool:
        return default

    def ask(self, question: str, *, default: str = "") -> str:
        return default


@dataclass
class _Parameter:
    """A literal the compiler decided to turn into a named input."""

    name: str
    example: str | None
    sensitive: bool


class Compiler:
    """Turns one finished recording into one capability artifact."""

    def __init__(self, prompter: Prompter | None = None) -> None:
        self.prompter = prompter or CliPrompter()
        #: Literal values recognised as secrets while compiling. Collected
        #: so they can be scrubbed from the artifact's free text, not just
        #: replaced in the step that typed them -- see _scrub().
        self._secrets: set[str] = set()

    def _scrub(self, text: str) -> str:
        """Remove secret literals from prose before it enters the artifact.

        Parameterizing the password keeps it out of `params`, which is the
        obvious leak. It is not the only one: a goal reading "log in as
        standard_user/secret_sauce" flows into the capability's id, name
        and description, and into provenance. Those are free text nobody
        thinks of as a credential store, which is exactly why the value
        survives there. Section 6 says a sensitive value is never written
        to the artifact in plaintext -- that has to mean anywhere in it.
        """
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _strip_secrets(self, text: str) -> str:
        """Drop secret literals entirely, for places a token cannot go.

        An identifier has to stay snake_case, so "[REDACTED]" is not
        available there -- the secret is removed rather than replaced.
        """
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, " ")
        return text

    def _safe_identifier(self, text: str) -> str:
        """A snake_case id with no secret in it, whatever was proposed."""
        slug = slugify(self._strip_secrets(text)).replace("-", "_").strip("_")
        if not slug or not slug[0].isalpha():
            slug = f"capability_{slug}".strip("_")
        return slug

    # -- entry point -------------------------------------------------------

    def compile(
        self,
        recording: Recording,
        *,
        capability_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Capability:
        """Distil the successful path of a recording into a capability."""
        executed = recording.successful_steps
        if not executed:
            raise ValueError(
                "nothing to compile: the recording contains no successfully "
                "executed steps"
            )

        inputs: dict[str, InputSpec] = {}
        steps = [
            self._compile_step(recorded, index, inputs)
            for index, recorded in enumerate(executed)
        ]
        outputs = [
            OutputSpec(name=step.output_name, source_step=step.step_id)
            for step in steps
            if step.output_name
        ]

        self._declare_business_outcomes(steps)

        # Scrubbed after the steps are compiled, because that pass is what
        # discovers which literals are secrets in the first place.
        identifier = capability_id or self._propose_id(recording)
        return Capability(
            capability_id=self._safe_identifier(identifier),
            name=self._scrub(name or self._propose_name(recording)),
            description=self._scrub(description or recording.summary or recording.goal),
            target=self._target(recording),
            inputs=list(inputs.values()),
            outputs=outputs,
            steps=steps,
            success_checkpoint=self._success_checkpoint(executed),
            provenance=Provenance(
                discovered_by=DiscoveredBy(
                    model=recording.model,
                    run_id=recording.run_id,
                    goal=self._scrub(recording.goal),
                )
            ),
        )

    # -- business outcomes -------------------------------------------------

    def _declare_business_outcomes(self, steps: list[Step]) -> None:
        """Ask what can legitimately go wrong, per step.

        Authored at compile time rather than derived, because a discovery
        run only ever sees the happy path -- it never had bad credentials
        to be rejected for. Section 2 puts these on the step deliberately:
        being asked here is what forces the question "what can go wrong at
        this point" while the flow is still in front of you.
        """
        if not self.prompter.confirm(
            "\ndeclare expected business outcomes (legitimate non-success results)?",
            default=False,
        ):
            return

        by_id = {step.step_id: step for step in steps}
        print("  known steps: " + ", ".join(by_id))
        while True:
            step_id = self.prompter.ask("  step_id (blank to finish):", default="")
            if not step_id:
                return
            step = by_id.get(step_id)
            if step is None:
                print(f"    no such step: {step_id!r}")
                continue
            value = self.prompter.ask("    text that appears on the page:", default="")
            code = self.prompter.ask("    outcome_code:", default="")
            if not value or not code:
                print("    skipped: both a match text and an outcome_code are needed")
                continue
            step.expected_business_outcomes.append(
                BusinessOutcome(
                    match=OutcomeMatch(type=MatchType.TEXT, value=value), outcome_code=code
                )
            )
            print(f"    added {code!r} to {step_id}")

    # -- steps -------------------------------------------------------------

    def _compile_step(
        self, recorded: RecordedStep, index: int, inputs: dict[str, InputSpec]
    ) -> Step:
        action = recorded.action
        assert action is not None

        locator = action.locator
        params: dict[str, object] = {}

        if locator is not None:
            locator = self._maybe_template_locator(locator, recorded, inputs)
        if action.value is not None:
            params["value"] = self._maybe_parameterize_value(action, recorded, inputs)
        if action.url is not None:
            params["url"] = action.url
        if action.condition is not None:
            params["condition"] = action.condition.value

        return Step(
            step_id=self._step_id(recorded, index),
            action=action.type,
            locator=locator,
            params=params,
            checkpoint=self._checkpoint(recorded),
            risk_level=action.risk_level,
            approved=action.approved,
            timeout_ms=action.timeout_ms,
            output_name=recorded.output_name,
        )

    def _step_id(self, recorded: RecordedStep, index: int) -> str:
        """A readable, stable id: the action plus what it acted on."""
        action = recorded.action
        assert action is not None
        if recorded.output_name:
            subject = recorded.output_name
        elif action.locator is not None and action.locator.tiers[0].name:
            subject = action.locator.tiers[0].name
        elif action.type is ActionType.NAVIGATE and action.url:
            subject = urlparse(action.url).path.strip("/") or "home"
        else:
            subject = str(index)
        slug = slugify(subject).replace("-", "_") or str(index)
        return f"{action.type.value}_{slug}"

    # -- parameters --------------------------------------------------------

    def _maybe_parameterize_value(
        self, action, recorded: RecordedStep, inputs: dict[str, InputSpec]
    ) -> str:
        """Offer a typed value as an input, or keep it as a literal."""
        literal = action.value
        field_name = action.locator.tiers[0].name if action.locator else None
        suggested = slugify(field_name or "value").replace("-", "_")
        sensitive = self._looks_sensitive(field_name)

        # Already a parameter. Discovery substitutes credentials at the
        # moment of typing and records the placeholder, so the recording
        # never held the literal -- there is nothing here to decide, and
        # nothing to ask about.
        existing = placeholders(literal)
        if existing:
            for name in existing:
                self._declare(
                    inputs, _Parameter(name=name, example=None, sensitive=sensitive)
                )
            return literal

        if sensitive:
            # Not offered as a choice. A secret cannot stay a literal in the
            # artifact, because the artifact is the thing it must stay out of.
            print(
                f"  {field_name!r} looks like a credential -- parameterizing as "
                f"{suggested!r} and marking it sensitive (its value is not written "
                "to the artifact)."
            )
            chosen = suggested
            self._secrets.add(literal)
        else:
            question = f"  treat {literal!r} (field {field_name!r}) as a parameter?"
            if not self.prompter.confirm(question, default=True):
                return literal
            chosen = self.prompter.ask("    input name:", default=suggested) or suggested

        self._declare(
            inputs,
            _Parameter(name=chosen, example=None if sensitive else literal, sensitive=sensitive),
        )
        return "${" + chosen + "}"

    def _maybe_template_locator(
        self, locator: Locator, recorded: RecordedStep, inputs: dict[str, InputSpec]
    ) -> Locator:
        """Offer to parameterize a product id embedded in a CSS fallback.

        See the module docstring: this is a saucedemo-shaped rule, kept
        deliberately narrow. It only fires when a name visible in the
        snapshot the step was taken against slugifies to a suffix of a CSS
        tier -- so the proposal is grounded in what was on the page, not in
        un-slugging a string back into a guess.
        """
        for position, spec in enumerate(locator.fallbacks):
            value = spec.get("value")
            if not isinstance(value, str) or not value.startswith("#"):
                continue
            found = self._templatable_suffix(value, recorded.snapshot)
            if found is None:
                continue
            literal, slug = found

            question = (
                f"  locator {value!r} contains {literal!r} -- treat it as a parameter?"
            )
            if not self.prompter.confirm(question, default=True):
                return locator
            chosen = self.prompter.ask("    input name:", default="item_name") or "item_name"

            templated = value[: -len(slug)] + "${" + chosen + "|slug}"
            fallbacks = list(locator.fallbacks)
            fallbacks[position] = {**spec, "value": templated}
            self._declare(inputs, _Parameter(name=chosen, example=literal, sensitive=False))
            return Locator(
                strategy=locator.strategy, primary=locator.primary, fallbacks=fallbacks
            )
        return locator

    @staticmethod
    def _templatable_suffix(
        css_value: str, snapshot: SurfaceSnapshot | None
    ) -> tuple[str, str] | None:
        """Find a label on the page whose slug ends this CSS selector."""
        if snapshot is None:
            return None
        for node in snapshot.nodes:
            for text in (node.name, node.value):
                if not text:
                    continue
                slug = slugify(text)
                if len(slug) >= MIN_SLUG_LENGTH and css_value.endswith(slug):
                    return text, slug
        return None

    @staticmethod
    def _looks_sensitive(field_name: str | None) -> bool:
        lowered = (field_name or "").lower()
        return any(hint in lowered for hint in SENSITIVE_HINTS)

    @staticmethod
    def _declare(inputs: dict[str, InputSpec], parameter: _Parameter) -> None:
        existing = inputs.get(parameter.name)
        if existing is not None:
            # Reused across steps: keep the stricter reading of both.
            if parameter.sensitive and not existing.sensitive:
                inputs[parameter.name] = InputSpec(name=parameter.name, sensitive=True)
            return
        inputs[parameter.name] = InputSpec(
            name=parameter.name,
            sensitive=parameter.sensitive,
            example=None if parameter.sensitive else parameter.example,
        )

    # -- checkpoints -------------------------------------------------------

    def _checkpoint(self, recorded: RecordedStep) -> Checkpoint | None:
        """Derive a post-condition from what the step actually changed.

        Only a navigation is inferred, because only a navigation leaves
        evidence strong enough to assert on. Inventing a checkpoint for a
        step that changed nothing observable would produce an assertion
        that passes whatever happens, which is worse than none: it reads
        like verification while verifying nothing.
        """
        before = recorded.snapshot.url if recorded.snapshot else None
        after = recorded.result.url if recorded.result else None
        if not after or not before:
            return None
        if urlparse(before).path == urlparse(after).path:
            return None
        return Checkpoint(type=CheckpointType.URL_CONTAINS, value=urlparse(after).path)

    def _success_checkpoint(self, executed: list[RecordedStep]) -> Checkpoint | None:
        final = next(
            (step.result.url for step in reversed(executed) if step.result and step.result.url),
            None,
        )
        if not final:
            return None
        return Checkpoint(type=CheckpointType.URL_CONTAINS, value=urlparse(final).path)

    # -- naming ------------------------------------------------------------

    def _target(self, recording: Recording) -> Target:
        parsed = urlparse(recording.target)
        host = parsed.hostname or "app"
        app_id = slugify(host.replace("www.", "").split(".")[0]).replace("-", "_")
        return Target(
            app_id=app_id,
            surface_type=SurfaceType.WEB,
            base_url=f"{parsed.scheme}://{parsed.netloc}",
        )

    def _propose_id(self, recording: Recording) -> str:
        """Suggest an id, with secrets removed *before* it is displayed.

        The prompt shows its default. A goal of "log in as
        standard_user/secret_sauce" would otherwise print the password to
        the terminal and into the operator's shell scrollback -- a leak
        that scrubbing the finished artifact would not undo.
        """
        goal = self._strip_secrets(recording.goal)
        suggested = f"{self._target(recording).app_id}_{_condense(goal)}"
        return self.prompter.ask("capability_id:", default=suggested) or suggested

    def _propose_name(self, recording: Recording) -> str:
        suggested = self._scrub(recording.goal).strip().rstrip(".")
        suggested = suggested[:1].upper() + suggested[1:]
        return self.prompter.ask("name:", default=suggested) or suggested


_STOPWORDS = frozenset(
    {"a", "an", "the", "to", "and", "then", "as", "in", "on", "of", "with", "for", "at"}
)


def _condense(goal: str, limit: int = 5) -> str:
    """A short snake_case handle from a sentence-long goal."""
    words = [w for w in re.findall(r"[a-z0-9]+", goal.lower()) if w not in _STOPWORDS]
    return "_".join(words[:limit]) or "capability"
