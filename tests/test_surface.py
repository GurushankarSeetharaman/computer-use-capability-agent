"""Unit tests for computer_use.surface -- no browser, no network.

Everything here runs against a fake page. That is possible because locator
resolution was deliberately kept free of Playwright imports, and it is
worth preserving: fallback ordering is the part of this system most likely
to hide an off-by-one, and it deserves tests that run in milliseconds.

Coverage of the live adapter (perceive/act against a real browser) is the
job of scripts/scratch_surface_check.py, which is a manual smoke check
rather than a test, per the build prompt.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from computer_use.surface.locators import build_handle, describe_tiers, resolve
from computer_use.surface.models import (
    Action,
    ActionType,
    Locator,
    LocatorSpec,
    LocatorStrategy,
    SurfaceSnapshot,
    WaitCondition,
    tier_label,
)


# -- fake page -------------------------------------------------------------


class _FakeHandle:
    """Stands in for a Playwright locator handle."""

    def __init__(self, key: tuple, resolvable: set[tuple]) -> None:
        self.key = key
        self._resolvable = resolvable

    @property
    def first(self) -> "_FakeHandle":
        return self

    def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        if self.key not in self._resolvable:
            raise TimeoutError(f"nothing matched {self.key}")


class FakePage:
    """A page where the caller declares exactly what exists.

    `raising` models a tier that is malformed rather than merely absent --
    an unknown ARIA role, say, which real Playwright rejects when the
    locator is built rather than when it is awaited.
    """

    def __init__(self, resolvable: set[tuple] = frozenset(), raising: set[tuple] = frozenset()):
        self._resolvable = set(resolvable)
        self._raising = set(raising)
        self.calls: list[tuple] = []

    def _handle(self, key: tuple) -> _FakeHandle:
        self.calls.append(key)
        if key in self._raising:
            raise ValueError(f"malformed locator: {key}")
        return _FakeHandle(key, self._resolvable)

    def get_by_role(self, role: str, **kwargs) -> _FakeHandle:
        return self._handle(("role", role, kwargs.get("name")))

    def get_by_text(self, text: str, **kwargs) -> _FakeHandle:
        return self._handle(("text", text))

    def locator(self, selector: str, **kwargs) -> _FakeHandle:
        return self._handle(("selector", selector))


# -- locator schema --------------------------------------------------------


def test_role_name_locator_requires_a_role() -> None:
    with pytest.raises(ValidationError):
        LocatorSpec(strategy=LocatorStrategy.ROLE_NAME, name="Login")


def test_value_strategies_require_a_value() -> None:
    with pytest.raises(ValidationError):
        LocatorSpec(strategy=LocatorStrategy.CSS)


def test_unknown_locator_field_is_rejected() -> None:
    """A typo must fail at load time, not silently produce a dead locator."""
    with pytest.raises(ValidationError):
        LocatorSpec(strategy=LocatorStrategy.CSS, selector="#user-name")


def test_tiers_are_primary_then_fallbacks_in_order() -> None:
    locator = Locator(
        strategy=LocatorStrategy.ROLE_NAME,
        primary={"role": "textbox", "name": "Username"},
        fallbacks=[
            {"strategy": "css", "value": "#user-name"},
            {"strategy": "xpath", "value": "//input[1]"},
        ],
    )
    assert [spec.strategy for spec in locator.tiers] == [
        LocatorStrategy.ROLE_NAME,
        LocatorStrategy.CSS,
        LocatorStrategy.XPATH,
    ]
    assert locator.tiers[0].name == "Username"
    assert locator.tiers[1].value == "#user-name"


def test_fallback_without_its_own_strategy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Locator(
            strategy=LocatorStrategy.ROLE_NAME,
            primary={"role": "button"},
            fallbacks=[{"value": "#login"}],
        )


def test_role_name_shorthand_builds_a_css_fallback() -> None:
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")
    assert len(locator.tiers) == 2
    assert locator.tiers[1].strategy is LocatorStrategy.CSS


def test_tier_labels_name_primary_and_fallbacks() -> None:
    assert tier_label(0) == "primary"
    assert tier_label(2) == "fallback_2"


# -- locator resolution ----------------------------------------------------


def test_build_handle_maps_each_strategy_to_its_page_method() -> None:
    page = FakePage()
    build_handle(page, LocatorSpec(strategy=LocatorStrategy.ROLE_NAME, role="button", name="Go"))
    build_handle(page, LocatorSpec(strategy=LocatorStrategy.CSS, value="#a"))
    build_handle(page, LocatorSpec(strategy=LocatorStrategy.XPATH, value="//b"))
    build_handle(page, LocatorSpec(strategy=LocatorStrategy.TEXT, value="hi"))
    assert page.calls == [
        ("role", "button", "Go"),
        ("selector", "#a"),
        ("selector", "xpath=//b"),
        ("text", "hi"),
    ]


def test_resolve_prefers_the_primary_tier() -> None:
    page = FakePage(resolvable={("role", "button", "Login"), ("selector", "#login-button")})
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")

    resolution = resolve(page, locator, probe_timeout_ms=10)

    assert resolution is not None
    assert resolution.tier == 0
    assert resolution.spec.strategy is LocatorStrategy.ROLE_NAME
    assert page.calls == [("role", "button", "Login")], "fallbacks must not be probed"


def test_resolve_falls_through_and_reports_the_tier_that_won() -> None:
    """The drift signal: which tier succeeded has to survive into the result."""
    page = FakePage(resolvable={("selector", "#login-button")})
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")

    resolution = resolve(page, locator, probe_timeout_ms=10)

    assert resolution is not None
    assert resolution.tier == 1
    assert tier_label(resolution.tier) == "fallback_1"
    assert resolution.spec.value == "#login-button"


def test_resolve_returns_none_when_no_tier_matches() -> None:
    page = FakePage()
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")
    assert resolve(page, locator, probe_timeout_ms=10) is None


def test_a_malformed_tier_costs_only_that_tier() -> None:
    """A bad selector in one tier must not abort the whole resolution."""
    page = FakePage(
        resolvable={("selector", "#login-button")},
        raising={("role", "nonsense-role", "Login")},
    )
    locator = Locator(
        strategy=LocatorStrategy.ROLE_NAME,
        primary={"role": "nonsense-role", "name": "Login"},
        fallbacks=[{"strategy": "css", "value": "#login-button"}],
    )

    resolution = resolve(page, locator, probe_timeout_ms=10)

    assert resolution is not None
    assert resolution.tier == 1


def test_describe_tiers_lists_every_attempt() -> None:
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")
    described = describe_tiers(locator)
    assert "role='button'" in described
    assert "#login-button" in described


# -- action validation -----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": ActionType.NAVIGATE},
        {"type": ActionType.CLICK},
        {"type": ActionType.TYPE, "locator": Locator.role_name("textbox", "Username")},
        {"type": ActionType.EXTRACT},
        {"type": ActionType.WAIT_FOR},
        {"type": ActionType.WAIT_FOR, "condition": WaitCondition.VISIBLE},
        {"type": ActionType.WAIT_FOR, "condition": WaitCondition.URL_CONTAINS},
    ],
    ids=[
        "navigate-without-url",
        "click-without-locator",
        "type-without-value",
        "extract-without-locator",
        "wait_for-without-condition",
        "wait_for-visible-without-locator",
        "wait_for-url_contains-without-value",
    ],
)
def test_incoherent_actions_are_rejected_before_reaching_a_browser(kwargs) -> None:
    with pytest.raises(ValidationError):
        Action(**kwargs)


def test_coherent_actions_are_accepted() -> None:
    Action(type=ActionType.NAVIGATE, url="https://www.saucedemo.com")
    Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Login"))
    Action(
        type=ActionType.TYPE,
        locator=Locator.role_name("textbox", "Username"),
        value="standard_user",
    )
    Action(
        type=ActionType.WAIT_FOR,
        condition=WaitCondition.URL_CONTAINS,
        value="/inventory.html",
    )


def test_action_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Action(type=ActionType.NAVIGATE, url="https://example.com", timeout_ms=0)


def test_action_describe_is_readable() -> None:
    action = Action(
        type=ActionType.TYPE,
        locator=Locator.role_name("textbox", "Username"),
        value="standard_user",
    )
    assert action.describe() == "type role='textbox' name='Username' value='standard_user'"


# -- snapshot --------------------------------------------------------------


def test_snapshot_renders_compactly_for_the_model() -> None:
    snapshot = SurfaceSnapshot(
        url="https://www.saucedemo.com/",
        title="Swag Labs",
        nodes=[
            {"role": "textbox", "name": "Username", "value": "standard_user"},
            {"role": "button", "name": "Login"},
        ],
    )
    rendered = snapshot.describe()
    assert "url: https://www.saucedemo.com/" in rendered
    assert "- textbox 'Username' = 'standard_user'" in rendered
    assert "- button 'Login'" in rendered


def test_surface_package_imports_without_playwright_installed() -> None:
    """The models must stay usable in an environment with no browser."""
    import computer_use.surface as surface

    assert surface.Action is Action
    assert surface.__doc__
