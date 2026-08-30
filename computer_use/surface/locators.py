"""Turning a Locator into something clickable, one tier at a time.

This module never imports Playwright. It takes any object exposing the
three page methods it needs, which keeps locator resolution -- the piece
most likely to harbour an off-by-one in the fallback order -- unit
testable against a fake page, with no browser and no network.

Resolution order is the whole point: primary first, then each fallback in
declaration order, reporting which tier won. That report is what turns
routine logging into a drift signal (design notes section 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from computer_use.surface.models import Locator, LocatorSpec, LocatorStrategy


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
    """A found element, plus the provenance of how it was found."""

    handle: Any
    tier: int
    spec: LocatorSpec


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
        return page.get_by_text(spec.value)
    raise ValueError(f"unsupported locator strategy: {spec.strategy}")


def resolve(
    page: PageLike, locator: Locator, *, probe_timeout_ms: int
) -> Resolution | None:
    """Return the first tier that actually matches an element, or None.

    Each tier gets its own bounded probe rather than sharing the action
    timeout. A locator with three fallbacks would otherwise take three
    times as long to fail as a locator with none, making a step timeout
    depend on how carefully its fallbacks were written -- which would be a
    perverse incentive against writing them.

    Any exception from a tier is treated as "not this tier" and moves on.
    That is broader than catching Playwright timeouts alone, and
    deliberately so: an invalid CSS selector in tier 2 should cost that
    tier, not the whole step. The tier that eventually succeeds is
    reported, so a silent degradation still shows up in the log.
    """
    for index, spec in enumerate(locator.tiers):
        try:
            handle = build_handle(page, spec).first
            handle.wait_for(state="attached", timeout=probe_timeout_ms)
        except Exception:
            continue
        return Resolution(handle=handle, tier=index, spec=spec)
    return None


def describe_tiers(locator: Locator) -> str:
    """Render every tier, for the failure message when none of them match."""
    return " | ".join(spec.describe() for spec in locator.tiers)
