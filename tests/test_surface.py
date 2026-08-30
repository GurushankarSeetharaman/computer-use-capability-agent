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
    TierOutcome,
    WaitCondition,
    tier_label,
)


# -- fake page -------------------------------------------------------------


class _FakeHandle:
    """Stands in for a Playwright locator handle.

    Models a match *count*, not just presence, because "matched more than
    one element" is now a distinct outcome from "matched nothing".
    """

    def __init__(self, key: tuple, counts: dict[tuple, int]) -> None:
        self.key = key
        self._counts = counts

    @property
    def first(self) -> "_FakeHandle":
        return self

    def wait_for(self, state: str | None = None, timeout: int | None = None) -> None:
        if self._counts.get(self.key, 0) < 1:
            raise TimeoutError(f"nothing matched {self.key}")

    def count(self) -> int:
        return self._counts.get(self.key, 0)


class FakePage:
    """A page where the caller declares exactly what exists, and how many.

    `resolvable` may be a set (each key matches exactly one element) or a
    dict of key -> match count, for testing ambiguity. `raising` models a
    tier that is malformed rather than merely absent -- an unknown ARIA
    role, say, which real Playwright rejects when the locator is built
    rather than when it is awaited.
    """

    def __init__(self, resolvable=frozenset(), raising: set[tuple] = frozenset()):
        if isinstance(resolvable, dict):
            self._counts = dict(resolvable)
        else:
            self._counts = {key: 1 for key in resolvable}
        self._raising = set(raising)
        self.calls: list[tuple] = []

    def _handle(self, key: tuple) -> _FakeHandle:
        self.calls.append(key)
        if key in self._raising:
            raise ValueError(f"malformed locator: {key}")
        return _FakeHandle(key, self._counts)

    def get_by_role(self, role: str, **kwargs) -> _FakeHandle:
        return self._handle(("role", role, kwargs.get("name")))

    def get_by_text(self, text, **kwargs) -> _FakeHandle:
        # Compiled patterns are keyed by their source, so assertions stay
        # readable and do not depend on re module caching identity.
        if hasattr(text, "pattern"):
            return self._handle(("regex", text.pattern))
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

    report = resolve(page, locator, probe_timeout_ms=10)

    assert report.resolved
    assert report.resolution.tier == 0
    assert report.resolution.spec.strategy is LocatorStrategy.ROLE_NAME
    assert page.calls == [("role", "button", "Login")], "fallbacks must not be probed"


def test_resolve_falls_through_and_reports_the_tier_that_won() -> None:
    """The drift signal: which tier succeeded has to survive into the result."""
    page = FakePage(resolvable={("selector", "#login-button")})
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")

    report = resolve(page, locator, probe_timeout_ms=10)

    assert report.resolved
    assert report.resolution.tier == 1
    assert tier_label(report.resolution.tier) == "fallback_1"
    assert report.resolution.spec.value == "#login-button"


def test_resolve_returns_none_when_no_tier_matches() -> None:
    page = FakePage()
    locator = Locator.role_name("button", "Login", css_fallback="#login-button")
    assert not resolve(page, locator, probe_timeout_ms=10).resolved


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

    report = resolve(page, locator, probe_timeout_ms=10)

    assert report.resolved
    assert report.resolution.tier == 1


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


def test_a_nameless_content_node_renders_as_content_not_as_a_name() -> None:
    """Rendering content with "=" invited an addressing strategy that cannot work.

    The snapshot's `text` nodes carry their words in `value`. Shown as
    `text = 'Total: $32.39'` the model read that as a name and addressed
    it with role+name, which never matches because `text` is not an
    addressable ARIA role.
    """
    snapshot = SurfaceSnapshot(
        url="https://www.saucedemo.com/checkout-step-two.html",
        title="Swag Labs",
        nodes=[
            {"role": "text", "value": "Total: $32.39"},
            {"role": "textbox", "name": "Zip", "value": "12345"},
        ],
    )
    rendered = snapshot.describe()
    assert "- text content: 'Total: $32.39'" in rendered
    assert "- textbox 'Zip' = '12345'" in rendered


def test_surface_package_imports_without_playwright_installed() -> None:
    """The models must stay usable in an environment with no browser."""
    import computer_use.surface as surface

    assert surface.Action is Action
    assert surface.__doc__


# -- ambiguity -------------------------------------------------------------


def test_a_tier_matching_several_elements_does_not_resolve() -> None:
    """The live bug: get_by_text('Total:') matched the item subtotal too.

    Taking .first returned a plausible wrong value with no error, which is
    worse than failing: replay would depend on document order.
    """
    page = FakePage(resolvable={("text", "Total:"): 2})
    locator = Locator(strategy=LocatorStrategy.TEXT, primary={"value": "Total:"})

    report = resolve(page, locator, probe_timeout_ms=10)

    assert not report.resolved
    assert [a.outcome for a in report.attempts] == [TierOutcome.AMBIGUOUS]
    assert report.attempts[0].matches == 2


def test_ambiguity_falls_through_to_the_next_tier() -> None:
    page = FakePage(resolvable={("role", "button", "Total"): 3, ("selector", "#total"): 1})
    locator = Locator.role_name("button", "Total", css_fallback="#total")

    report = resolve(page, locator, probe_timeout_ms=10)

    assert report.resolved
    assert report.resolution.tier == 1
    assert [a.outcome for a in report.attempts] == [
        TierOutcome.AMBIGUOUS,
        TierOutcome.MATCHED,
    ]


def test_every_attempt_is_recorded_even_on_success() -> None:
    """The drift signal lives in the tiers that failed on the way."""
    page = FakePage(resolvable={("role", "button", "Login"): 4, ("selector", "#login"): 1})
    locator = Locator.role_name("button", "Login", css_fallback="#login")

    report = resolve(page, locator, probe_timeout_ms=10)

    assert len(report.attempts) == 2
    assert len(report.ambiguous) == 1
    assert report.ambiguous[0].label == "primary"
    assert report.ambiguous[0].matches == 4


def test_a_tier_that_cannot_be_counted_is_treated_as_failed() -> None:
    """Unable to prove uniqueness is not the same as proven unique."""

    class Uncountable(_FakeHandle):
        def count(self):
            raise RuntimeError("cannot count")

    class UncountablePage(FakePage):
        def _handle(self, key):
            self.calls.append(key)
            return Uncountable(key, {key: 1})

    locator = Locator(strategy=LocatorStrategy.CSS, primary={"value": "#x"})
    report = resolve(UncountablePage(), locator, probe_timeout_ms=10)

    assert not report.resolved
    assert report.attempts[0].outcome is TierOutcome.ERROR


def test_the_report_describes_the_whole_ladder() -> None:
    """This text is what the agent sees, so it must say *why* each tier lost."""
    page = FakePage(resolvable={("role", "button", "Total"): 2})
    locator = Locator.role_name("button", "Total", css_fallback="#total")

    described = resolve(page, locator, probe_timeout_ms=10).describe()

    assert "primary" in described
    assert "ambiguous" in described
    assert "2 matches" in described
    assert "fallback_1" in described


# -- regex text matching ---------------------------------------------------


def test_a_regex_text_spec_compiles_the_pattern() -> None:
    page = FakePage(resolvable={("regex", "^Total: "): 1})
    spec = LocatorSpec(strategy=LocatorStrategy.TEXT, value="^Total: ", regex=True)

    build_handle(page, spec)

    assert page.calls == [("regex", "^Total: ")]


def test_an_anchored_pattern_resolves_what_a_substring_could_not() -> None:
    """The live failure: 'Total:' also matched 'Item total: $29.99'."""
    ambiguous = Locator(strategy=LocatorStrategy.TEXT, primary={"value": "Total:"})
    anchored = Locator(
        strategy=LocatorStrategy.TEXT, primary={"value": "^Total: ", "regex": True}
    )
    page = FakePage(resolvable={("text", "Total:"): 2, ("regex", "^Total: "): 1})

    assert not resolve(page, ambiguous, probe_timeout_ms=10).resolved
    assert resolve(page, anchored, probe_timeout_ms=10).resolved


def test_regex_is_rejected_on_strategies_that_cannot_use_it() -> None:
    with pytest.raises(ValidationError):
        LocatorSpec(strategy=LocatorStrategy.CSS, value="#total", regex=True)
    with pytest.raises(ValidationError):
        LocatorSpec(strategy=LocatorStrategy.ROLE_NAME, role="button", regex=True)


def test_an_invalid_pattern_fails_when_the_locator_is_built() -> None:
    """A bad pattern must not wait until mid-replay to announce itself."""
    with pytest.raises(ValidationError):
        LocatorSpec(strategy=LocatorStrategy.TEXT, value="^Total: [", regex=True)


def test_a_regex_spec_describes_itself_distinguishably() -> None:
    spec = LocatorSpec(strategy=LocatorStrategy.TEXT, value="^Total: ", regex=True)
    plain = LocatorSpec(strategy=LocatorStrategy.TEXT, value="Total:")
    assert spec.describe() == "text~='^Total: '"
    assert plain.describe() == "text='Total:'"
