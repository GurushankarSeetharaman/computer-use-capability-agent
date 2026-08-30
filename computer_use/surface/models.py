"""Typed vocabulary shared by everything that touches a surface.

These models are deliberately free of any Playwright import. Two reasons:
they are the contract the artifact schema serialises (design notes section 2),
so they must survive a swap to a desktop adapter; and keeping them importable
without a browser installed means the majority of this package can be unit
tested without launching one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class LocatorStrategy(str, Enum):
    """How to find an element, in the priority order of design notes section 3.

    Role+name leads because an accessible role and name survive markup
    churn that breaks CSS and xpath, and because the same pair exists in a
    desktop accessibility tree -- which is what lets one artifact schema
    describe both surfaces.
    """

    ROLE_NAME = "role+name"
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"


class LocatorSpec(BaseModel):
    """One concrete way to find an element: a single locator tier.

    Forbidding extra fields is deliberate -- a typo such as
    {"strategy": "css", "selector": "#x"} should fail when the artifact
    loads, not silently produce a valueless locator that fails
    mysteriously in the middle of a replay.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: LocatorStrategy
    role: str | None = None
    name: str | None = None
    value: str | None = None

    @model_validator(mode="after")
    def _require_strategy_fields(self) -> LocatorSpec:
        if self.strategy is LocatorStrategy.ROLE_NAME:
            if not self.role:
                raise ValueError("role+name locators require a 'role'")
        elif not self.value:
            raise ValueError(f"{self.strategy.value} locators require a 'value'")
        return self

    def describe(self) -> str:
        """Human-readable form, for logs and failure messages."""
        if self.strategy is LocatorStrategy.ROLE_NAME:
            return f"role={self.role!r} name={self.name!r}"
        return f"{self.strategy.value}={self.value!r}"


class Locator(BaseModel):
    """A primary way to find an element, plus ordered fallbacks.

    The wire format matches the artifact schema in design notes section 2
    exactly -- the top-level strategy describes ``primary``, and each
    fallback carries its own strategy. That shape is normalised once, at
    construction, into ``tiers``: an ordered list the adapter walks.
    Normalising here means a malformed locator raises when the artifact is
    loaded rather than when replay finally reaches the step that uses it.
    """

    strategy: LocatorStrategy
    primary: dict[str, Any]
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)

    _tiers: list[LocatorSpec] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _build_tiers(self) -> Locator:
        tiers = [LocatorSpec(strategy=self.strategy, **self.primary)]
        for position, fallback in enumerate(self.fallbacks, start=1):
            spec = dict(fallback)
            strategy = spec.pop("strategy", None)
            if strategy is None:
                raise ValueError(
                    f"fallback {position} must name its own 'strategy'; "
                    "a fallback may use a different strategy from the primary"
                )
            tiers.append(LocatorSpec(strategy=strategy, **spec))
        self._tiers = tiers
        return self

    @property
    def tiers(self) -> list[LocatorSpec]:
        """Primary first, then each fallback, in the order they are tried."""
        return self._tiers

    @classmethod
    def role_name(
        cls, role: str, name: str | None = None, *, css_fallback: str | None = None
    ) -> Locator:
        """Shorthand for the common case, to keep call sites readable."""
        fallbacks = [{"strategy": "css", "value": css_fallback}] if css_fallback else []
        return cls(
            strategy=LocatorStrategy.ROLE_NAME,
            primary={"role": role, "name": name},
            fallbacks=fallbacks,
        )


def tier_label(index: int) -> str:
    """Name a tier for the evidence log: 0 is primary, the rest fall back.

    Logged per step because a capability that starts resolving on tier 2 is
    telling you the target app has drifted (design notes section 4).
    """
    return "primary" if index == 0 else f"fallback_{index}"


class BoundingBox(BaseModel):
    """Where an element sits on screen, for grounding against a screenshot."""

    x: float
    y: float
    width: float
    height: float


class A11yNode(BaseModel):
    """One interactive or labelled node from the accessibility tree.

    Carries exactly what a decision needs -- what kind of thing it is, what
    it is called, what it currently holds, and where it is -- and nothing
    about how it is implemented in the DOM. That omission is the point: it
    keeps the model reasoning in the same vocabulary the locators use, so a
    decision it makes is expressible as a locator without translation.
    """

    role: str
    name: str | None = None
    value: str | None = None
    bounding_box: BoundingBox | None = None

    def describe(self) -> str:
        parts = [self.role]
        if self.name:
            parts.append(repr(self.name))
        if self.value:
            parts.append(f"= {self.value!r}")
        return " ".join(parts)


class SurfaceSnapshot(BaseModel):
    """What the automation can see right now: the return value of perceive().

    Pruned rather than complete, on purpose. A full accessibility tree is
    mostly structural scaffolding, and paying context tokens for it makes
    decisions worse, not better.
    """

    url: str
    title: str
    nodes: list[A11yNode] = Field(default_factory=list)
    screenshot_path: str | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def describe(self) -> str:
        """Compact text rendering -- what the agent loop will show Claude."""
        lines = [f"url: {self.url}", f"title: {self.title}", "elements:"]
        lines.extend(f"  - {node.describe()}" for node in self.nodes)
        return "\n".join(lines)


class ActionType(str, Enum):
    """The verbs a surface understands.

    Kept small and closed. This same list becomes Claude's tool schema in
    discovery and the permitted step actions in an artifact, so every
    addition has to be justified in three places at once -- which is the
    friction we want against the surface sprouting ad-hoc verbs.
    """

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    EXTRACT = "extract"
    WAIT_FOR = "wait_for"


class WaitCondition(str, Enum):
    """Bounded conditions to wait on. Never a bare sleep (design notes section 3)."""

    VISIBLE = "visible"
    TEXT_PRESENT = "text_present"
    URL_CONTAINS = "url_contains"


class Action(BaseModel):
    """One thing to do to the page.

    Per-type requirements are enforced here rather than in the adapter, so
    that an incoherent action (a click with nothing to click) is rejected
    before it reaches a browser -- and so the guardrail, which runs between
    this model and the adapter, is always inspecting a coherent object.
    """

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    locator: Locator | None = None
    url: str | None = None
    value: str | None = None
    condition: WaitCondition | None = None
    timeout_ms: int = Field(default=5000, gt=0)

    @model_validator(mode="after")
    def _require_fields_for_type(self) -> Action:
        needs_locator = {
            ActionType.CLICK,
            ActionType.TYPE,
            ActionType.SELECT,
            ActionType.EXTRACT,
        }
        needs_value = {ActionType.TYPE, ActionType.SELECT}

        if self.type is ActionType.NAVIGATE and not self.url:
            raise ValueError("navigate requires a 'url'")
        if self.type in needs_locator and self.locator is None:
            raise ValueError(f"{self.type.value} requires a 'locator'")
        if self.type in needs_value and self.value is None:
            raise ValueError(f"{self.type.value} requires a 'value'")
        if self.type is ActionType.WAIT_FOR:
            if self.condition is None:
                raise ValueError("wait_for requires a 'condition'")
            if self.condition is WaitCondition.VISIBLE and self.locator is None:
                raise ValueError("wait_for visible requires a 'locator'")
            if self.condition is not WaitCondition.VISIBLE and not self.value:
                raise ValueError(f"wait_for {self.condition.value} requires a 'value'")
        return self

    def describe(self) -> str:
        """One-line form for logs; the evidence trail is read by humans."""
        if self.type is ActionType.NAVIGATE:
            return f"navigate -> {self.url}"
        if self.type is ActionType.WAIT_FOR:
            condition = self.condition.value if self.condition else "?"
            target = self.locator.tiers[0].describe() if self.locator else self.value
            return f"wait_for {condition} ({target})"
        target = self.locator.tiers[0].describe() if self.locator else "?"
        suffix = f" value={self.value!r}" if self.value is not None else ""
        return f"{self.type.value} {target}{suffix}"


class ActionResult(BaseModel):
    """What happened, including which locator tier got there.

    Returned rather than raised, even on failure. Callers -- the replay
    engine especially -- have to classify an outcome as a business outcome,
    a recoverable hiccup, or a hard failure (design notes section 3).
    Handing them an exception would push that judgement down into this
    layer, which lacks the artifact context needed to make it.
    """

    action_type: ActionType
    succeeded: bool
    locator_tier: int | None = None
    locator_tier_label: str | None = None
    locator_strategy: LocatorStrategy | None = None
    tiers_attempted: int = 0
    extracted: str | None = None
    url: str | None = None
    error: str | None = None
    duration_ms: int = 0

    @classmethod
    def failure(
        cls, action: Action, error: str, *, duration_ms: int = 0, **extra: Any
    ) -> ActionResult:
        """Build a failed result without repeating the boilerplate."""
        return cls(
            action_type=action.type,
            succeeded=False,
            error=error,
            duration_ms=duration_ms,
            **extra,
        )
