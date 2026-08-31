"""Tests for the replay engine.

The classification tests run against a fake surface, which is the only way
to cover them properly: "the page showed an error banner", "a spinner was
still up on the second attempt", "the locator went ambiguous" are states
you cannot summon on demand against a live site, and they are exactly the
states whose handling matters.

The live pair that Prompt 6 asks for -- success and business_outcome
against the real artifact -- is at the bottom, marked `live` and excluded
from the default run. See pytest.ini.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from computer_use.artifact import (
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
    Target,
    load,
)
from computer_use.replay import (
    InputValidationError,
    ReplayEngine,
    ReplayStatus,
    find_pattern,
    validate_inputs,
)
from computer_use.surface.models import (
    Action,
    ActionResult,
    ActionType,
    Locator,
    LocatorStrategy,
    SurfaceSnapshot,
)

SAUCEDEMO = "https://www.saucedemo.com"
ARTIFACT = (
    Path(__file__).resolve().parent.parent
    / "artifacts"
    / "examples"
    / "saucedemo_add_item_to_cart_checkout.json"
)


# -- fake surface ----------------------------------------------------------


@dataclass
class FakeSurface:
    """A surface whose pages are scripted, one per perceive()."""

    pages: list[SurfaceSnapshot] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    _last: SurfaceSnapshot | None = None

    def perceive(self, *, screenshot: bool = True) -> SurfaceSnapshot:
        if self.pages:
            self._last = self.pages.pop(0)
        if self._last is None:
            self._last = SurfaceSnapshot(url=SAUCEDEMO, title="t")
        return self._last

    def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        if self.results:
            return self.results.pop(0)
        return ActionResult(action_type=action.type, succeeded=True, url=SAUCEDEMO)


def page(url: str, *nodes) -> SurfaceSnapshot:
    return SurfaceSnapshot(url=url, title="Swag Labs", nodes=list(nodes))


def _capability(steps: list[Step], **overrides) -> Capability:
    base = dict(
        capability_id="demo",
        name="Demo",
        description="d",
        target=Target(app_id="saucedemo", base_url=SAUCEDEMO),
        inputs=[],
        outputs=[],
        steps=steps,
        provenance=Provenance(discovered_by=DiscoveredBy(model="m", run_id="r")),
    )
    base.update(overrides)
    return Capability(**base)


def _login_step(**overrides) -> Step:
    base = dict(
        step_id="click_login",
        action=ActionType.CLICK,
        locator=Locator.role_name("button", "Login"),
        checkpoint=Checkpoint(type=CheckpointType.URL_CONTAINS, value="/inventory.html"),
    )
    base.update(overrides)
    return Step(**base)


def engine(surface: FakeSurface, tmp_path: Path, **kwargs) -> ReplayEngine:
    return ReplayEngine(surface, evidence_root=str(tmp_path), backoff_s=0.0, **kwargs)


# -- input validation ------------------------------------------------------


def test_inputs_are_validated_against_the_declared_contract() -> None:
    capability = _capability(
        [Step(step_id="s", action=ActionType.TYPE, params={"value": "${username}"},
              locator=Locator.role_name("textbox", "Username"))],
        inputs=[InputSpec(name="username")],
    )
    assert validate_inputs(capability, {"username": "standard_user"}) == {
        "username": "standard_user"
    }

    with pytest.raises(InputValidationError):
        validate_inputs(capability, {})


def test_an_unknown_argument_is_rejected_rather_than_ignored() -> None:
    """`user_name` for `username` should be heard about immediately."""
    capability = _capability(
        [Step(step_id="s", action=ActionType.TYPE, params={"value": "${username}"},
              locator=Locator.role_name("textbox", "Username"))],
        inputs=[InputSpec(name="username")],
    )
    with pytest.raises(InputValidationError):
        validate_inputs(capability, {"username": "a", "user_name": "b"})


# -- the three statuses ----------------------------------------------------


def test_a_clean_run_returns_success_with_outputs(tmp_path: Path) -> None:
    step = Step(
        step_id="extract_order_total",
        action=ActionType.EXTRACT,
        locator=Locator(strategy=LocatorStrategy.TEXT, primary={"value": "Total:"}),
        output_name="order_total",
    )
    surface = FakeSurface(
        pages=[page(f"{SAUCEDEMO}/checkout-step-two.html")],
        results=[
            ActionResult(
                action_type=ActionType.EXTRACT, succeeded=True, extracted="Total: $32.39"
            )
        ],
    )
    capability = _capability(
        [step],
        outputs=[OutputSpec(name="order_total", source_step="extract_order_total")],
        success_checkpoint=Checkpoint(
            type=CheckpointType.URL_CONTAINS, value="/checkout-step-two.html"
        ),
    )

    result = engine(surface, tmp_path).run(capability, {})

    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs == {"order_total": "Total: $32.39"}
    assert result.to_contract() == {
        "status": "success",
        "outputs": {"order_total": "Total: $32.39"},
    }


def test_a_declared_business_outcome_is_not_a_failure(tmp_path: Path) -> None:
    """The application said no. That is an answer, not a defect."""
    step = _login_step(
        expected_business_outcomes=[
            BusinessOutcome(
                match=OutcomeMatch(type=MatchType.TEXT, value="Epic sadface"),
                outcome_code="invalid_credentials",
            )
        ]
    )
    surface = FakeSurface(
        pages=[page(SAUCEDEMO, {"role": "text", "value": "Epic sadface: no match"})]
    )

    result = engine(surface, tmp_path).run(_capability([step]), {})

    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.code == "invalid_credentials"
    assert result.to_contract()["status"] == "business_outcome"
    assert "step_id" not in result.to_contract()


def test_an_unclassifiable_mismatch_is_a_failure_with_evidence(tmp_path: Path) -> None:
    surface = FakeSurface(pages=[page(f"{SAUCEDEMO}/somewhere-else.html")])

    result = engine(surface, tmp_path).run(_capability([_login_step()]), {})

    assert result.status is ReplayStatus.FAILURE
    assert result.step_id == "click_login"
    assert result.expected == "url_contains='/inventory.html'"
    assert "/somewhere-else.html" in result.observed
    assert Path(result.evidence_path).exists()


def test_classification_prefers_a_business_outcome_over_a_retry(tmp_path: Path) -> None:
    """Retrying a rejected password is useless, and can lock an account."""
    step = _login_step(
        expected_business_outcomes=[
            BusinessOutcome(
                match=OutcomeMatch(type=MatchType.TEXT, value="Epic sadface"),
                outcome_code="invalid_credentials",
            )
        ]
    )
    # The page shows BOTH a rejection and a spinner: the rejection wins.
    surface = FakeSurface(
        pages=[
            page(
                SAUCEDEMO,
                {"role": "text", "value": "Epic sadface: no match"},
                {"role": "progressbar", "name": "Loading"},
            )
        ]
    )

    result = engine(surface, tmp_path).run(_capability([step]), {})

    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert len(surface.actions) == 1, "no retry once the answer is known"


# -- recovery --------------------------------------------------------------


def test_a_transient_busy_state_is_retried_then_succeeds(tmp_path: Path) -> None:
    surface = FakeSurface(
        pages=[
            page(SAUCEDEMO, {"role": "progressbar", "name": "Loading"}),
            page(f"{SAUCEDEMO}/inventory.html"),
        ]
    )

    result = engine(surface, tmp_path).run(_capability([_login_step()]), {})

    assert result.status is ReplayStatus.SUCCESS
    assert len(surface.actions) == 2, "the step was retried once"


def test_retries_are_bounded(tmp_path: Path) -> None:
    """A recovery list that retries forever hides failures instead of fixing them."""
    busy = [page(SAUCEDEMO, {"role": "progressbar", "name": "Loading"}) for _ in range(10)]
    surface = FakeSurface(pages=busy)

    result = engine(surface, tmp_path, max_retries=2).run(_capability([_login_step()]), {})

    assert result.status is ReplayStatus.FAILURE
    assert len(surface.actions) == 3, "initial attempt plus max_retries"


def test_a_dismissible_interstitial_is_closed_before_retrying(tmp_path: Path) -> None:
    surface = FakeSurface(
        pages=[
            page(
                SAUCEDEMO,
                {"role": "dialog", "name": "Cookies"},
                {"role": "button", "name": "Close"},
            ),
            page(f"{SAUCEDEMO}/inventory.html"),
        ]
    )

    result = engine(surface, tmp_path).run(_capability([_login_step()]), {})

    assert result.status is ReplayStatus.SUCCESS
    dismissed = [a for a in surface.actions if a.locator and a.locator.tiers[0].name == "Close"]
    assert dismissed, "the interstitial was dismissed, not just waited out"


def test_consent_buttons_are_not_treated_as_dismissal() -> None:
    """Closing a dialog is recovery; agreeing to it is a decision."""
    consent = page(SAUCEDEMO, {"role": "dialog", "name": "Terms"},
                   {"role": "button", "name": "Accept all"})
    assert find_pattern(consent) is None


def test_a_policy_refusal_is_never_retried(tmp_path: Path) -> None:
    surface = FakeSurface(
        pages=[page(SAUCEDEMO, {"role": "progressbar", "name": "Loading"})],
        results=[
            ActionResult.blocked_by_guardrail(
                Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Login")),
                reason="irreversible action has no prior approval",
                needs_escalation=True,
            )
        ],
    )

    result = engine(surface, tmp_path).run(_capability([_login_step()]), {})

    assert result.status is ReplayStatus.FAILURE
    assert len(surface.actions) == 1, "the answer will not change on a second try"


# -- evidence --------------------------------------------------------------


def test_the_locator_tier_reaches_the_evidence_log(tmp_path: Path) -> None:
    """Section 4's drift signal has to be in the file, not just on stdout."""
    surface = FakeSurface(
        pages=[page(f"{SAUCEDEMO}/inventory.html")],
        results=[
            ActionResult(
                action_type=ActionType.CLICK,
                succeeded=True,
                locator_tier=1,
                locator_tier_label="fallback_1",
                locator_strategy=LocatorStrategy.CSS,
            )
        ],
    )

    engine(surface, tmp_path).run(_capability([_login_step()]), {})

    logged = (tmp_path / "replay_" ).parent
    lines = [
        line
        for path in logged.rglob("log.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any('"locator_tier": "fallback_1"' in line for line in lines)
    assert any('"locator_strategy": "css"' in line for line in lines)


def test_a_sensitive_value_never_reaches_the_log(tmp_path: Path) -> None:
    capability = _capability(
        [
            Step(
                step_id="type_password",
                action=ActionType.TYPE,
                locator=Locator.role_name("textbox", "Password"),
                params={"value": "${password}"},
            )
        ],
        inputs=[InputSpec(name="password", sensitive=True)],
    )
    surface = FakeSurface(pages=[page(SAUCEDEMO)])

    engine(surface, tmp_path).run(capability, {"password": "hunter2"})

    written = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("log.jsonl")
    )
    assert "hunter2" not in written


# -- templating through replay --------------------------------------------


def test_inputs_are_rendered_into_params_and_locators(tmp_path: Path) -> None:
    capability = _capability(
        [
            Step(
                step_id="click_add_to_cart",
                action=ActionType.CLICK,
                locator=Locator(
                    strategy=LocatorStrategy.ROLE_NAME,
                    primary={"role": "button", "name": "Add to cart"},
                    fallbacks=[
                        {"strategy": "css", "value": "#add-to-cart-${item_name|slug}"}
                    ],
                ),
            )
        ],
        inputs=[InputSpec(name="item_name")],
    )
    surface = FakeSurface(pages=[page(SAUCEDEMO)])

    engine(surface, tmp_path).run(capability, {"item_name": "Sauce Labs Bike Light"})

    rendered = surface.actions[0].locator.tiers[1].value
    assert rendered == "#add-to-cart-sauce-labs-bike-light"


# -- live: the pair Prompt 6 asks for --------------------------------------


@pytest.mark.live
def test_live_replay_succeeds_with_valid_inputs() -> None:
    """Replays the committed artifact against the real site."""
    from computer_use.guardrail import AllowlistConfig
    from computer_use.surface import PlaywrightSurface

    capability = load(ARTIFACT)
    allowlist = AllowlistConfig.from_file("config/allowlist.saucedemo.json")

    with PlaywrightSurface(allowlist=allowlist, screenshot_dir="evidence/_live") as surface:
        surface.page.goto(SAUCEDEMO, wait_until="domcontentloaded")
        result = ReplayEngine(surface).run(
            capability,
            {
                "username": "standard_user",
                "password": "secret_sauce",
                "item_name": "Sauce Labs Backpack",
                "first_name": "John",
                "last_name": "Doe",
                "zip_postal_code": "12345",
            },
        )

    assert result.status is ReplayStatus.SUCCESS, result.to_contract()
    assert "order_total" in result.outputs


@pytest.mark.live
def test_live_replay_with_a_wrong_username_is_a_business_outcome() -> None:
    """The rejection is an answer, not a defect -- section 3's central claim."""
    from computer_use.guardrail import AllowlistConfig
    from computer_use.surface import PlaywrightSurface

    capability = load(ARTIFACT)
    allowlist = AllowlistConfig.from_file("config/allowlist.saucedemo.json")

    with PlaywrightSurface(allowlist=allowlist, screenshot_dir="evidence/_live") as surface:
        surface.page.goto(SAUCEDEMO, wait_until="domcontentloaded")
        result = ReplayEngine(surface).run(
            capability,
            {
                "username": "not_a_real_user",
                "password": "secret_sauce",
                "item_name": "Sauce Labs Backpack",
                "first_name": "John",
                "last_name": "Doe",
                "zip_postal_code": "12345",
            },
        )

    assert result.status is ReplayStatus.BUSINESS_OUTCOME, result.to_contract()
    assert result.code == "invalid_credentials"
