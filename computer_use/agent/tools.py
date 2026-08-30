"""Claude's tool surface, and the translation from a tool call to an Action.

The tool list is a near mirror of ActionType plus two control verbs, so a
decision the model makes maps onto exactly one thing the surface can do.
Nothing here interprets intent: if a tool call cannot become a valid
Action, that is an error handed back to the model, not a guess made on its
behalf.

One deliberate constraint: elements are addressed by accessible role and
name, and CSS may only be supplied as a *fallback*. The model cannot reach
for a selector as its primary strategy. That is the locator-priority rule
of design notes section 3 enforced at the boundary where it is easiest to
enforce -- if CSS were merely discouraged in the prompt, a capability
recorded on a bad day would bake a brittle selector into an artifact that
outlives the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from computer_use.surface.models import (
    Action,
    ActionType,
    Locator,
    RiskLevel,
    WaitCondition,
)

DONE = "done"
REPORT_STUCK = "report_stuck"

_RISK_PROPERTY = {
    "type": "string",
    "enum": [level.value for level in RiskLevel],
    "description": (
        "How much damage this action does if it is wrong. 'safe' for reads and "
        "navigation, 'reversible' for changes that can be undone (adding to a "
        "cart), 'irreversible' for anything that submits, pays, commits or "
        "deletes. Irreversible actions stop for human approval, so mark them "
        "honestly: an unmarked irreversible action is the one failure this "
        "system cannot catch for you."
    ),
}

_TARGET_PROPERTIES = {
    "role": {
        "type": "string",
        "description": "Accessible role of the element, exactly as shown in the snapshot (e.g. button, textbox, link).",
    },
    "name": {
        "type": "string",
        "description": "Accessible name of the element, exactly as shown in the snapshot.",
    },
    "css_fallback": {
        "type": "string",
        "description": (
            "Optional CSS selector used ONLY if role+name stops matching later. "
            "It is a safety net for replay, never the primary way to find an element."
        ),
    },
}


def _target_schema(extra: dict[str, Any] | None = None, required: list[str] | None = None) -> dict:
    properties = {**_TARGET_PROPERTIES, "risk_level": _RISK_PROPERTY, **(extra or {})}
    return {
        "type": "object",
        "properties": properties,
        "required": ["role", *(required or [])],
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Load a URL. Use this once at the start; prefer clicking links afterwards.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "risk_level": _RISK_PROPERTY},
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click one element, addressed by accessible role and name.",
        "input_schema": _target_schema(),
    },
    {
        "name": "type",
        "description": "Type a value into a field. The field is cleared first.",
        "input_schema": _target_schema(
            {"value": {"type": "string", "description": "Text to enter."}},
            required=["value"],
        ),
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown by its value.",
        "input_schema": _target_schema(
            {"value": {"type": "string", "description": "Option value to select."}},
            required=["value"],
        ),
    },
    {
        "name": "extract",
        "description": (
            "Read text or a field value from an element. Use this for every value the "
            "goal asks you to report back, so it can become a named output of the "
            "resulting capability."
        ),
        "input_schema": _target_schema(
            {
                "output_name": {
                    "type": "string",
                    "description": (
                        "Snake_case name for this value in the capability's outputs, "
                        "e.g. cart_total."
                    ),
                }
            },
            required=["output_name"],
        ),
    },
    {
        "name": "wait_for",
        "description": (
            "Wait for a bounded condition before continuing. Use after an action that "
            "triggers navigation or a page update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "enum": [condition.value for condition in WaitCondition],
                },
                "value": {
                    "type": "string",
                    "description": "Text to look for, or URL fragment, depending on condition.",
                },
                "role": {"type": "string", "description": "Required when condition is 'visible'."},
                "name": {"type": "string", "description": "Used when condition is 'visible'."},
                "risk_level": _RISK_PROPERTY,
            },
            "required": ["condition"],
        },
    },
    {
        "name": DONE,
        "description": (
            "Call this when the goal is fully achieved. Report the values you extracted "
            "so they become the capability's outputs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One or two sentences on what was accomplished.",
                },
                "outputs": {
                    "type": "object",
                    "description": "Named values produced by the run, e.g. {\"cart_total\": \"$29.99\"}.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": REPORT_STUCK,
        "description": (
            "Call this when you cannot make progress: the page is not what you expected, "
            "an element you need is missing, or you would be guessing. Escalating is a "
            "correct outcome, not a failure -- do it rather than trying variations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "What you expected, what you saw, and what you tried.",
                }
            },
            "required": ["reason"],
        },
    },
]

#: Tool names that end the loop rather than driving the browser.
CONTROL_TOOLS = frozenset({DONE, REPORT_STUCK})

_ACTION_TOOLS = {
    "navigate": ActionType.NAVIGATE,
    "click": ActionType.CLICK,
    "type": ActionType.TYPE,
    "select": ActionType.SELECT,
    "extract": ActionType.EXTRACT,
    "wait_for": ActionType.WAIT_FOR,
}


@dataclass
class Decision:
    """One turn's choice by the model, translated but not yet carried out."""

    tool_name: str
    payload: dict[str, Any]
    action: Action | None = None
    output_name: str | None = None
    summary: str | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    reason: str | None = None

    @property
    def is_done(self) -> bool:
        return self.tool_name == DONE

    @property
    def is_stuck(self) -> bool:
        return self.tool_name == REPORT_STUCK


def _locator(payload: dict[str, Any]) -> Locator:
    return Locator.role_name(
        payload["role"],
        payload.get("name"),
        css_fallback=payload.get("css_fallback"),
    )


def _risk(payload: dict[str, Any]) -> RiskLevel:
    return RiskLevel(payload.get("risk_level", RiskLevel.SAFE.value))


def interpret(tool_name: str, payload: dict[str, Any], *, timeout_ms: int = 5000) -> Decision:
    """Turn one tool call into a Decision, or raise ValueError explaining why not.

    Raising rather than repairing is deliberate. A malformed tool call is
    something the model can see and correct on the next turn if we tell it
    plainly; silently filling in a missing field would produce an action
    nobody chose, recorded as though someone had.
    """
    if tool_name == DONE:
        return Decision(
            tool_name=tool_name,
            payload=payload,
            summary=payload.get("summary", ""),
            outputs={str(k): str(v) for k, v in (payload.get("outputs") or {}).items()},
        )
    if tool_name == REPORT_STUCK:
        return Decision(
            tool_name=tool_name, payload=payload, reason=payload.get("reason", "")
        )

    action_type = _ACTION_TOOLS.get(tool_name)
    if action_type is None:
        raise ValueError(f"unknown tool {tool_name!r}")

    common = {"type": action_type, "risk_level": _risk(payload), "timeout_ms": timeout_ms}

    if action_type is ActionType.NAVIGATE:
        if not payload.get("url"):
            raise ValueError("navigate requires 'url'")
        return Decision(tool_name, payload, action=Action(url=payload["url"], **common))

    if action_type is ActionType.WAIT_FOR:
        return Decision(tool_name, payload, action=_wait_action(payload, common))

    if not payload.get("role"):
        raise ValueError(f"{tool_name} requires 'role'")

    action = Action(
        locator=_locator(payload),
        value=payload.get("value"),
        **common,
    )
    return Decision(tool_name, payload, action=action, output_name=payload.get("output_name"))


def _wait_action(payload: dict[str, Any], common: dict[str, Any]) -> Action:
    """Build a wait_for action, whose required fields depend on the condition."""
    raw = payload.get("condition")
    try:
        condition = WaitCondition(raw)
    except ValueError as exc:
        raise ValueError(f"unknown wait condition {raw!r}") from exc

    if condition is WaitCondition.VISIBLE:
        if not payload.get("role"):
            raise ValueError("wait_for 'visible' requires 'role'")
        return Action(condition=condition, locator=_locator(payload), **common)
    if not payload.get("value"):
        raise ValueError(f"wait_for {condition.value!r} requires 'value'")
    return Action(condition=condition, value=payload["value"], **common)
