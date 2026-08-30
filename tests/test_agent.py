"""Unit tests for the discovery loop.

The Anthropic client is injected, so every branch below runs against
scripted responses -- no API key, no network, no browser. That matters
more here than it looks: the parts of a discovery loop most likely to be
wrong are the ones that only show up on a bad day (a refused action, a
malformed tool call, a model that stops calling tools), and none of those
are reproducible on demand against the live API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from computer_use.agent import (
    TOOL_SCHEMAS,
    DiscoveryAgent,
    DiscoveryOutcome,
    Recording,
    StepOutcome,
    interpret,
)
from computer_use.surface.models import (
    Action,
    ActionResult,
    ActionType,
    LocatorStrategy,
    RiskLevel,
    SurfaceSnapshot,
    WaitCondition,
)


# -- scripted doubles ------------------------------------------------------


@dataclass
class FakeToolUse:
    name: str
    input: dict[str, Any]
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeText:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list[Any]
    stop_reason: str = "tool_use"


class FakeMessages:
    def __init__(self, script: list[FakeResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("the loop asked for more turns than were scripted")
        return self._script.pop(0)


class FakeClient:
    def __init__(self, script: list[FakeResponse]) -> None:
        self.messages = FakeMessages(script)


@dataclass
class FakeSurface:
    """A surface that reports whatever the test tells it to."""

    results: list[ActionResult] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    perceives: int = 0

    def perceive(self, *, screenshot: bool = True) -> SurfaceSnapshot:
        self.perceives += 1
        return SurfaceSnapshot(
            url="https://www.saucedemo.com/",
            title="Swag Labs",
            nodes=[{"role": "button", "name": "Login"}],
        )

    def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        if self.results:
            return self.results.pop(0)
        return ActionResult(action_type=action.type, succeeded=True, url="https://www.saucedemo.com/")


def agent(script: list[FakeResponse], surface: FakeSurface | None = None, **kwargs: Any):
    surface = surface or FakeSurface()
    return DiscoveryAgent(surface, client=FakeClient(script), **kwargs), surface


def click_call(**payload: Any) -> FakeToolUse:
    return FakeToolUse(name="click", input={"role": "button", "name": "Login", **payload})


DONE_CALL = FakeToolUse(
    name="done", input={"summary": "Reached checkout.", "outputs": {"cart_total": "$29.99"}}
)


# -- tool schema -----------------------------------------------------------


def test_every_action_type_has_a_matching_tool() -> None:
    """The model's vocabulary and the surface's must not drift apart."""
    names = {tool["name"] for tool in TOOL_SCHEMAS}
    for action_type in ActionType:
        assert action_type.value in names
    assert {"done", "report_stuck"} <= names


def test_every_tool_declares_a_schema_and_description() -> None:
    for tool in TOOL_SCHEMAS:
        assert tool["description"].strip()
        assert tool["input_schema"]["type"] == "object"
        assert "required" in tool["input_schema"]


def test_css_is_only_offered_as_a_fallback_never_as_a_primary_locator() -> None:
    """The locator-priority rule of section 3, enforced at the tool boundary."""
    for tool in TOOL_SCHEMAS:
        properties = tool["input_schema"]["properties"]
        assert "css" not in properties
        assert "selector" not in properties
        if "css_fallback" in properties:
            assert "css_fallback" not in tool["input_schema"]["required"]


# -- translation -----------------------------------------------------------


def test_click_becomes_a_role_name_action_with_a_css_fallback() -> None:
    decision = interpret("click", {"role": "button", "name": "Login", "css_fallback": "#login"})
    action = decision.action
    assert action is not None
    assert action.type is ActionType.CLICK
    assert action.locator is not None
    assert action.locator.tiers[0].role == "button"
    assert action.locator.tiers[1].value == "#login"


def test_risk_level_defaults_to_safe_and_is_honoured_when_given() -> None:
    assert interpret("click", {"role": "button"}).action.risk_level is RiskLevel.SAFE
    marked = interpret("click", {"role": "button", "risk_level": "irreversible"})
    assert marked.action.risk_level is RiskLevel.IRREVERSIBLE


def test_extract_carries_the_output_name_for_the_compiler() -> None:
    decision = interpret("extract", {"text": "Total:", "output_name": "cart_total"})
    assert decision.output_name == "cart_total"


def test_extract_by_text_uses_the_text_locator_strategy() -> None:
    """Content nodes have no accessible name to match on."""
    decision = interpret("extract", {"text": "Total:", "output_name": "order_total"})
    locator = decision.action.locator
    assert locator.strategy is LocatorStrategy.TEXT
    assert locator.tiers[0].value == "Total:"


def test_extract_by_role_still_works_for_labelled_controls() -> None:
    decision = interpret("extract", {"role": "textbox", "name": "Zip", "output_name": "zip"})
    assert decision.action.locator.strategy is LocatorStrategy.ROLE_NAME


def test_the_pseudo_role_that_broke_the_live_run_is_translated_not_failed() -> None:
    """Regression: `extract role='text' name='Total: $32.39'` used to fail.

    `text` is not an addressable ARIA role, so get_by_role could never
    match it -- the run reached its goal but sourced no output from a
    step. It is now translated to the text strategy, which section 3
    already lists as a locator tier.
    """
    decision = interpret(
        "extract",
        {"role": "text", "name": "Total: $32.39", "output_name": "order_total"},
    )
    locator = decision.action.locator
    assert locator.strategy is LocatorStrategy.TEXT
    assert locator.tiers[0].value == "Total: $32.39"


def test_a_pseudo_role_with_nothing_to_match_on_is_rejected() -> None:
    with pytest.raises(ValueError, match="visible text"):
        interpret("extract", {"role": "text", "output_name": "total"})


def test_extract_needs_something_to_address_and_an_output_name() -> None:
    with pytest.raises(ValueError, match="output_name"):
        interpret("extract", {"text": "Total:"})
    with pytest.raises(ValueError, match="'text' or 'role'"):
        interpret("extract", {"output_name": "total"})


def test_a_css_fallback_survives_onto_a_text_locator() -> None:
    decision = interpret(
        "extract",
        {"text": "Total:", "css_fallback": ".summary_total_label", "output_name": "t"},
    )
    tiers = decision.action.locator.tiers
    assert [tier.strategy for tier in tiers] == [LocatorStrategy.TEXT, LocatorStrategy.CSS]


def test_extract_does_not_require_a_role_in_its_schema() -> None:
    schema = next(t for t in TOOL_SCHEMAS if t["name"] == "extract")["input_schema"]
    assert schema["required"] == ["output_name"]
    assert "text" in schema["properties"]


def test_wait_for_requires_the_field_its_condition_needs() -> None:
    visible = interpret("wait_for", {"condition": "visible", "role": "button"})
    assert visible.action.condition is WaitCondition.VISIBLE
    url = interpret("wait_for", {"condition": "url_contains", "value": "/inventory.html"})
    assert url.action.value == "/inventory.html"

    with pytest.raises(ValueError):
        interpret("wait_for", {"condition": "url_contains"})
    with pytest.raises(ValueError):
        interpret("wait_for", {"condition": "visible"})


def test_malformed_calls_raise_rather_than_being_repaired() -> None:
    """Filling in a missing field would record an action nobody chose."""
    with pytest.raises(ValueError):
        interpret("navigate", {})
    with pytest.raises(ValueError):
        interpret("click", {"name": "Login"})
    with pytest.raises(ValueError):
        interpret("nonsense", {})


def test_done_and_report_stuck_translate_to_control_decisions() -> None:
    done = interpret("done", {"summary": "ok", "outputs": {"total": "$1"}})
    assert done.is_done and done.outputs == {"total": "$1"}
    stuck = interpret("report_stuck", {"reason": "no such element"})
    assert stuck.is_stuck and stuck.reason == "no such element"


# -- loop outcomes ---------------------------------------------------------


def test_done_ends_the_run_with_outputs() -> None:
    loop, _ = agent([FakeResponse([click_call()]), FakeResponse([DONE_CALL])])
    result = loop.run("add a backpack to the cart", "https://www.saucedemo.com")

    assert result.outcome is DiscoveryOutcome.GOAL_MET
    assert result.succeeded
    assert result.outputs == {"cart_total": "$29.99"}
    assert result.recording.finished_at is not None


def test_report_stuck_ends_the_run_and_escalates() -> None:
    stuck = FakeToolUse(name="report_stuck", input={"reason": "the cart link is missing"})
    loop, _ = agent([FakeResponse([stuck])])
    result = loop.run("goal", "https://www.saucedemo.com")

    assert result.outcome is DiscoveryOutcome.STUCK
    assert result.intervention is not None
    assert result.intervention.reason == "the cart link is missing"
    assert result.intervention.capability_or_goal == "goal"


def test_max_steps_stops_the_run() -> None:
    loop, _ = agent([FakeResponse([click_call()]) for _ in range(10)], max_steps=3)
    result = loop.run("goal", "https://www.saucedemo.com")

    assert result.outcome is DiscoveryOutcome.MAX_STEPS
    assert len(result.recording.steps) == 3


def test_timeout_stops_the_run() -> None:
    loop, _ = agent([FakeResponse([click_call()]) for _ in range(5)], timeout_s=-1.0)
    result = loop.run("goal", "https://www.saucedemo.com")
    assert result.outcome is DiscoveryOutcome.TIMEOUT


def test_a_model_that_stops_calling_tools_is_nudged_once_then_escalates() -> None:
    loop, _ = agent(
        [FakeResponse([FakeText("I think we are done here.")], stop_reason="end_turn")] * 2
    )
    result = loop.run("goal", "https://www.saucedemo.com")

    assert result.outcome is DiscoveryOutcome.STUCK
    assert result.intervention is not None


# -- recording -------------------------------------------------------------


def test_the_recording_keeps_failures_and_refusals_not_just_the_happy_path() -> None:
    """The compiler needs the noise present so that dropping it is deliberate."""
    surface = FakeSurface(
        results=[
            ActionResult.blocked_by_guardrail(
                _click(),
                reason="origin 'https://evil.example' is not allowlisted",
                needs_escalation=False,
            ),
            ActionResult(action_type=ActionType.CLICK, succeeded=False, error="TimeoutError"),
            ActionResult(action_type=ActionType.CLICK, succeeded=True),
        ]
    )
    loop, _ = agent(
        [
            FakeResponse([click_call()]),
            FakeResponse([click_call()]),
            FakeResponse([click_call()]),
            FakeResponse([DONE_CALL]),
        ],
        surface=surface,
    )
    result = loop.run("goal", "https://www.saucedemo.com")

    outcomes = [step.outcome for step in result.recording.steps]
    assert outcomes == [
        StepOutcome.BLOCKED,
        StepOutcome.FAILED,
        StepOutcome.EXECUTED,
        StepOutcome.CONTROL,
    ]
    assert len(result.recording.successful_steps) == 1


def test_an_invalid_tool_call_is_recorded_and_reported_back_to_the_model() -> None:
    loop, surface = agent(
        [
            FakeResponse([FakeToolUse(name="click", input={"name": "Login"})]),
            FakeResponse([DONE_CALL]),
        ]
    )
    result = loop.run("goal", "https://www.saucedemo.com")

    first = result.recording.steps[0]
    assert first.outcome is StepOutcome.INVALID_TOOL_CALL
    assert "role" in (first.error or "")
    assert surface.actions == [], "a malformed call must never reach the browser"


def test_a_refused_action_is_explained_back_to_the_model() -> None:
    surface = FakeSurface(
        results=[
            ActionResult.blocked_by_guardrail(
                _click(), reason="action type 'click' is not permitted", needs_escalation=False
            )
        ]
    )
    loop, _ = agent(
        [FakeResponse([click_call()]), FakeResponse([DONE_CALL])], surface=surface
    )
    loop.run("goal", "https://www.saucedemo.com")

    sent = loop.client.messages.calls[-1]["messages"]
    tool_results = [m for m in sent if m["role"] == "user" and isinstance(m["content"], list)]
    text = tool_results[-1]["content"][0]["content"]
    assert "REFUSED BY POLICY" in text
    assert "not permitted" in text


# -- API contract ----------------------------------------------------------


def test_only_one_action_runs_per_turn_but_every_tool_call_is_answered() -> None:
    """An unanswered tool_use block is a hard 400 on the next request."""
    two = FakeResponse(
        [
            FakeToolUse(name="click", input={"role": "button", "name": "Login"}, id="a"),
            FakeToolUse(name="click", input={"role": "button", "name": "Cart"}, id="b"),
        ]
    )
    loop, surface = agent([two, FakeResponse([DONE_CALL])])
    loop.run("goal", "https://www.saucedemo.com")

    assert len(surface.actions) == 1, "exactly one action per turn"

    sent = loop.client.messages.calls[-1]["messages"]
    results = [m for m in sent if m["role"] == "user" and isinstance(m["content"], list)][-1]
    answered = {block["tool_use_id"] for block in results["content"]}
    assert answered == {"a", "b"}


def test_the_assistant_turn_is_echoed_back_verbatim() -> None:
    """Thinking blocks and tool_use ids must survive unchanged across turns."""
    response = FakeResponse([click_call()])
    loop, _ = agent([response, FakeResponse([DONE_CALL])])
    loop.run("goal", "https://www.saucedemo.com")

    sent = loop.client.messages.calls[-1]["messages"]
    assistant = [m for m in sent if m["role"] == "assistant"][0]
    assert assistant["content"] is response.content


def test_requests_carry_the_named_model_tools_and_adaptive_thinking() -> None:
    loop, _ = agent([FakeResponse([DONE_CALL])], model="claude-sonnet-5")
    loop.run("goal", "https://www.saucedemo.com")

    request = loop.client.messages.calls[0]
    assert request["model"] == "claude-sonnet-5"
    assert request["tools"] is TOOL_SCHEMAS
    assert request["thinking"] == {"type": "adaptive"}
    assert "tool_choice" not in request, "Claude picks its own tool"


def test_one_perception_per_turn() -> None:
    """Perceiving twice would double the screenshots in the evidence trail."""
    loop, surface = agent(
        [FakeResponse([click_call()]), FakeResponse([click_call()]), FakeResponse([DONE_CALL])]
    )
    loop.run("goal", "https://www.saucedemo.com")
    # One opening perception, then one per executed turn.
    assert surface.perceives == 3


def _click() -> Action:
    from computer_use.surface.models import Locator

    return Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Login"))


def test_recording_round_trips_through_json() -> None:
    """The compiler and the evidence log both read this off disk."""
    loop, _ = agent([FakeResponse([click_call()]), FakeResponse([DONE_CALL])])
    result = loop.run("goal", "https://www.saucedemo.com")

    restored = Recording.model_validate_json(result.recording.model_dump_json())
    assert restored.run_id == result.recording.run_id
    assert len(restored.steps) == len(result.recording.steps)
    assert restored.outputs == {"cart_total": "$29.99"}
