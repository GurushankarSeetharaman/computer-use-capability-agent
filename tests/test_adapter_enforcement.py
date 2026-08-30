"""The guardrail is a mechanism in the adapter, not a convention.

These tests import Playwright but never launch a browser, and that is the
whole trick: the surface under test is deliberately left unstarted, so any
attempt to touch the page raises SurfaceNotStarted. An action that comes
back cleanly blocked therefore proves the policy check ran *before*
anything reached the browser -- had the ordering been the other way round,
these tests would error instead of passing.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from computer_use.guardrail import AllowlistConfig
from computer_use.surface.adapter import PlaywrightSurface, SurfaceNotStarted
from computer_use.surface.models import (
    Action,
    ActionType,
    Locator,
    RiskLevel,
)

SAUCEDEMO = "https://www.saucedemo.com"


@pytest.fixture
def allowlist() -> AllowlistConfig:
    return AllowlistConfig(
        allowed_base_urls=[SAUCEDEMO],
        allowed_routes=["/", "/inventory.html"],
        allowed_action_types=[ActionType.NAVIGATE, ActionType.CLICK],
    )


@pytest.fixture
def surface(allowlist: AllowlistConfig) -> PlaywrightSurface:
    """An unstarted surface: reaching the page from here is an error."""
    return PlaywrightSurface(allowlist=allowlist)


def test_a_surface_cannot_be_built_without_a_policy() -> None:
    """No default allowlist exists, so there is no unguarded surface."""
    with pytest.raises(TypeError):
        PlaywrightSurface()  # type: ignore[call-arg]

    signature = inspect.signature(PlaywrightSurface.__init__)
    assert signature.parameters["allowlist"].default is inspect.Parameter.empty


def test_offsite_navigation_is_blocked_before_the_browser_is_touched(
    surface: PlaywrightSurface,
) -> None:
    result = surface.act(Action(type=ActionType.NAVIGATE, url="https://evil.example/"))

    assert result.blocked
    assert not result.succeeded
    assert not result.needs_escalation
    assert "not allowlisted" in (result.guardrail_reason or "")


def test_disallowed_action_type_is_blocked(surface: PlaywrightSurface) -> None:
    action = Action(
        type=ActionType.TYPE,
        locator=Locator.role_name("textbox", "Username"),
        value="standard_user",
    )
    result = surface.act(action)

    assert result.blocked
    assert "action type" in (result.guardrail_reason or "")


def test_irreversible_action_escalates_rather_than_executing(
    surface: PlaywrightSurface,
) -> None:
    """Blocked, but flagged as needing a person -- not as out of bounds."""
    action = Action(
        type=ActionType.NAVIGATE,
        url=f"{SAUCEDEMO}/inventory.html",
        risk_level=RiskLevel.IRREVERSIBLE,
    )
    result = surface.act(action)

    assert result.blocked
    assert result.needs_escalation
    assert "human" in (result.guardrail_reason or "")


def test_a_blocked_result_is_distinguishable_from_a_failure(
    surface: PlaywrightSurface,
) -> None:
    """A failure may be worth retrying; a refusal never is."""
    blocked = surface.act(Action(type=ActionType.NAVIGATE, url="https://evil.example/"))
    assert blocked.blocked and not blocked.succeeded

    # Permitted by policy, so enforcement lets it through -- and it then
    # fails on the missing browser, which is the proof that the check is a
    # gate in front of execution rather than a replacement for it.
    permitted = Action(type=ActionType.NAVIGATE, url=f"{SAUCEDEMO}/inventory.html")
    executed = surface.act(permitted)
    assert not executed.blocked
    assert not executed.succeeded
    assert "SurfaceNotStarted" in (executed.error or "")


def test_perceive_is_not_gated_by_policy(surface: PlaywrightSurface) -> None:
    """Observation is never blocked; it would blind the evidence trail."""
    with pytest.raises(SurfaceNotStarted):
        surface.perceive()


def test_the_committed_saucedemo_policy_permits_the_demo_flow() -> None:
    """Guards against a policy file that quietly blocks the demo itself."""
    allowlist = AllowlistConfig.from_file(
        Path(__file__).resolve().parent.parent / "config" / "allowlist.saucedemo.json"
    )
    surface = PlaywrightSurface(allowlist=allowlist)
    result = surface.act(Action(type=ActionType.NAVIGATE, url=SAUCEDEMO))
    assert not result.blocked
