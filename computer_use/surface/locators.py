"""Turning a Locator into something clickable, one tier at a time.

This module never imports Playwright. It takes any object exposing the
three page methods it needs, which keeps locator resolution -- the piece
most likely to harbour an off-by-one in the fallback order -- unit
testable against a fake page, with no browser and no network.

Resolution order is the whole point: primary first, then each fallback in
declaration order, reporting which one won. That report is what turns
routine logging into a drift signal (design notes section 4).

A tier must identify **exactly one** element to count as resolved. Several
matches is treated as a failure of that tier, not as an invitation to take
the first one: picking by document order makes a capability depend on where
an element happens to sit on the page today, which is exactly the kind of
incidental detail replay is supposed to survive. It also fails silently --
the wrong element is read, a plausible value comes back, and nothing looks
broken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from computer_use.surface.models import (
    Locator,
    LocatorSpec,
    LocatorStrategy,
    TierAttempt,
    TierOutcome,
    tier_label,
)


class PageLike(Protocol):
    """The slice of a Playwright Page that locator resolution depends on.

    Declaring it as a Protocol rather than importing Page documents the
    real coupling -- three methods -- and lets tests substitute a fake.
    """

    def get_by_role(self, role: str, **kwargs: Any) -> Any: ...

    def get_by_text(self, text: str, **kwargs: Any) -> Any: ...

    def locator(self, selector: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class Resolution:
    """A uniquely identified element, plus how it was found."""

    handle: Any
    tier: int
    spec: LocatorSpec


@dataclass(frozen=True)
class ResolutionReport:
    """The outcome of walking the tiers, whether or not one succeeded.

    Carries every attempt even on success, because the drift signal is in
    the tiers that *failed* on the way to the one that worked.
    """

    resolution: Resolution | None
    attempts: list[TierAttempt] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.resolution is not None

    @property
    def ambiguous(self) -> list[TierAttempt]:
        """Tiers that matched more than one element."""
        return [a for a in self.attempts if a.outcome is TierOutcome.AMBIGUOUS]

    def describe(self) -> str:
        """The whole ladder, for a failure message or an evidence line."""
        return " | ".join(attempt.describe() for attempt in self.attempts)


def build_handle(page: PageLike, spec: LocatorSpec) -> Any:
    """Translate one spec into a Playwright locator handle.

    No waiting or matching happens here -- Playwright locators are lazy, so
    this is pure translation and is safe to call for tiers that will never
    be used.
    """
    if spec.strategy is LocatorStrategy.ROLE_NAME:
        if spec.name:
            return page.get_by_role(spec.role, name=spec.name)
        return page.get_by_role(spec.role)
    if spec.strategy is LocatorStrategy.CSS:
        return page.locator(spec.value)
    if spec.strategy is LocatorStrategy.XPATH:
        return page.locator(f"xpath={spec.value}")
    if spec.strategy is LocatorStrategy.TEXT:
        # A compiled pattern is matched against the element's text; an
        # anchored one can name a single line that a substring cannot.
        return page.get_by_text(re.compile(spec.value) if spec.regex else spec.value)
    raise ValueError(f"unsupported locator strategy: {spec.strategy}")


def _attempt(
    index: int, spec: LocatorSpec, outcome: TierOutcome, matches: int | None = None
) -> TierAttempt:
    return TierAttempt(
        tier=index,
        label=tier_label(index),
        strategy=spec.strategy,
        outcome=outcome,
        matches=matches,
    )


def resolve(page: PageLike, locator: Locator, *, probe_timeout_ms: int) -> ResolutionReport:
    """Walk the tiers, returning the first that identifies exactly one element.

    Each tier gets its own bounded probe rather than sharing the action
    timeout. A locator with three fallbacks would otherwise take three
    times as long to fail as a locator with none, making a step timeout
    depend on how carefully its fallbacks were written -- which would be a
    perverse incentive against writing them.

    Any exception from a tier is treated as that tier failing, and nothing
    more. That is broader than catching timeouts alone, and deliberately
    so: an invalid CSS selector in tier 2 should cost that tier, not the
    whole step. Every attempt is recorded either way.
    """
    attempts: list[TierAttempt] = []

    for index, spec in enumerate(locator.tiers):
        try:
            handle = build_handle(page, spec)
            handle.first.wait_for(state="attached", timeout=probe_timeout_ms)
        except Exception:
            attempts.append(_attempt(index, spec, TierOutcome.NO_MATCH))
            continue

        try:
            matches = handle.count()
        except Exception:
            # Unable to establish uniqueness. Treated as a failed tier for
            # the same reason the guardrail denies an unverifiable target:
            # "probably one element" is not a basis for a deterministic step.
            attempts.append(_attempt(index, spec, TierOutcome.ERROR))
            continue

        if matches > 1:
            attempts.append(_attempt(index, spec, TierOutcome.AMBIGUOUS, matches))
            continue

        attempts.append(_attempt(index, spec, TierOutcome.MATCHED, matches))
        return ResolutionReport(
            resolution=Resolution(handle=handle.first, tier=index, spec=spec),
            attempts=attempts,
        )

    return ResolutionReport(resolution=None, attempts=attempts)


def describe_tiers(locator: Locator) -> str:
    """Render every tier, for the failure message when none of them match."""
    return " | ".join(spec.describe() for spec in locator.tiers)
