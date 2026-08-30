"""Tests for the capability schema, the compiler, and the store.

The schema tests matter disproportionately: an artifact that references a
step that does not exist, or an input nobody declared, is broken in a way
that only surfaces partway through a replay -- possibly after something
irreversible. Every one of those below is a failure moved from run time to
load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from computer_use.agent.recording import (
    DiscoveryOutcome,
    RecordedStep,
    Recording,
    StepOutcome,
)
from computer_use.artifact import (
    Capability,
    Checkpoint,
    CheckpointType,
    Compiler,
    DiscoveredBy,
    InputSpec,
    NonInteractivePrompter,
    OutputSpec,
    Provenance,
    Step,
    Target,
    load,
    placeholders,
    render,
    save,
    slugify,
)
from computer_use.surface.models import (
    Action,
    ActionResult,
    ActionType,
    Locator,
    LocatorStrategy,
    RiskLevel,
    SurfaceSnapshot,
)

SAUCEDEMO = "https://www.saucedemo.com"


# -- templating ------------------------------------------------------------


def test_slugify_matches_saucedemo_element_ids() -> None:
    assert slugify("Sauce Labs Backpack") == "sauce-labs-backpack"
    assert slugify("Zip/Postal Code") == "zip-postal-code"


def test_render_substitutes_plain_and_slugged_placeholders() -> None:
    values = {"username": "standard_user", "item_name": "Sauce Labs Backpack"}
    assert render("${username}", values) == "standard_user"
    assert render("#add-to-cart-${item_name|slug}", values) == "#add-to-cart-sauce-labs-backpack"


def test_render_names_the_missing_input_rather_than_leaving_the_placeholder() -> None:
    """A leftover placeholder would fail later as "element not found"."""
    with pytest.raises(KeyError, match="item_name"):
        render("#add-to-cart-${item_name|slug}", {})


def test_placeholders_finds_every_referenced_input() -> None:
    assert placeholders("${a} and ${b|slug}") == {"a", "b"}
    assert placeholders("no placeholders here") == set()


# -- schema validation -----------------------------------------------------


def _capability(**overrides) -> dict:
    base = dict(
        capability_id="demo_capability",
        name="Demo",
        description="A demo capability.",
        target=Target(app_id="saucedemo", base_url=SAUCEDEMO),
        inputs=[InputSpec(name="username", example="standard_user")],
        outputs=[],
        steps=[
            Step(
                step_id="type_username",
                action=ActionType.TYPE,
                locator=Locator.role_name("textbox", "Username"),
                params={"value": "${username}"},
            )
        ],
        provenance=Provenance(discovered_by=DiscoveredBy(model="m", run_id="r")),
    )
    base.update(overrides)
    return base


def test_a_well_formed_capability_validates() -> None:
    assert Capability(**_capability()).capability_id == "demo_capability"


def test_capability_id_must_be_snake_case() -> None:
    with pytest.raises(ValidationError, match="snake_case"):
        Capability(**_capability(capability_id="Demo Capability"))


def test_duplicate_step_ids_are_rejected() -> None:
    step = Step(step_id="same", action=ActionType.NAVIGATE, params={"url": SAUCEDEMO})
    with pytest.raises(ValidationError, match="duplicate step_id"):
        Capability(**_capability(steps=[step, step.model_copy()], inputs=[]))


def test_an_output_naming_a_missing_step_is_rejected() -> None:
    """Unfixable at replay: there is no step to take the value from."""
    with pytest.raises(ValidationError, match="not a step"):
        Capability(
            **_capability(outputs=[OutputSpec(name="total", source_step="nonexistent")])
        )


def test_a_step_referencing_an_undeclared_input_is_rejected() -> None:
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability(**_capability(inputs=[]))


def test_a_declared_but_unused_input_is_rejected() -> None:
    """A declared input nothing consumes is a lie about the contract."""
    with pytest.raises(ValidationError, match="unused input"):
        Capability(
            **_capability(
                inputs=[
                    InputSpec(name="username"),
                    InputSpec(name="never_used"),
                ]
            )
        )


def test_a_placeholder_inside_a_locator_is_validated_too() -> None:
    """Locator templating must not be a hole in the contract check."""
    step = Step(
        step_id="click_add",
        action=ActionType.CLICK,
        locator=Locator(
            strategy=LocatorStrategy.ROLE_NAME,
            primary={"role": "button", "name": "Add to cart"},
            fallbacks=[{"strategy": "css", "value": "#add-to-cart-${item_name|slug}"}],
        ),
    )
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability(**_capability(steps=[step], inputs=[]))


def test_a_sensitive_input_cannot_carry_an_example() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        InputSpec(name="password", sensitive=True, example="secret_sauce")


# -- store -----------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    capability = Capability(**_capability())
    path = save(capability, tmp_path)
    assert path.name == "demo_capability.json"
    assert load(path) == capability


def test_a_malformed_artifact_fails_loudly_on_load(tmp_path: Path) -> None:
    """Not at replay time, when a browser is already open."""
    path = tmp_path / "broken.json"
    path.write_text('{"capability_id": "x"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        load(path)


# -- compiler --------------------------------------------------------------


def _executed(action: Action, *, url_before: str, url_after: str, output_name=None, nodes=()):
    return RecordedStep(
        index=0,
        outcome=StepOutcome.EXECUTED,
        snapshot=SurfaceSnapshot(url=url_before, title="t", nodes=list(nodes)),
        action=action,
        result=ActionResult(action_type=action.type, succeeded=True, url=url_after),
        output_name=output_name,
    )


def _recording(steps: list[RecordedStep], goal="log in and check out") -> Recording:
    recording = Recording(
        run_id="run_test", goal=goal, target=SAUCEDEMO, model="claude-sonnet-5", steps=steps
    )
    recording.outcome = DiscoveryOutcome.GOAL_MET
    return recording


def _compile(recording: Recording, **kwargs) -> Capability:
    return Compiler(NonInteractivePrompter()).compile(recording, **kwargs)


def test_only_the_successful_path_is_compiled() -> None:
    """Refusals, failures and dead ends stay in the recording, not the artifact."""
    good = _executed(
        Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Login")),
        url_before=SAUCEDEMO,
        url_after=f"{SAUCEDEMO}/inventory.html",
    )
    blocked = RecordedStep(
        index=1,
        outcome=StepOutcome.BLOCKED,
        action=Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Nope")),
        result=ActionResult(action_type=ActionType.CLICK, succeeded=False, blocked=True),
    )
    failed = RecordedStep(
        index=2,
        outcome=StepOutcome.FAILED,
        action=Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Gone")),
        result=ActionResult(action_type=ActionType.CLICK, succeeded=False),
    )

    capability = _compile(_recording([good, blocked, failed]))

    assert [step.step_id for step in capability.steps] == ["click_login"]


def test_a_recording_with_no_successful_steps_will_not_compile() -> None:
    failed = RecordedStep(index=0, outcome=StepOutcome.FAILED)
    with pytest.raises(ValueError, match="no successfully executed steps"):
        _compile(_recording([failed]))


def test_a_credential_field_is_parameterized_without_being_asked() -> None:
    """A secret cannot stay a literal in the artifact it must stay out of."""
    step = _executed(
        Action(
            type=ActionType.TYPE,
            locator=Locator.role_name("textbox", "Password"),
            value="secret_sauce",
        ),
        url_before=SAUCEDEMO,
        url_after=SAUCEDEMO,
    )
    # The prompter says no to everything; the password is parameterized anyway.
    capability = _compile(_recording([step]))

    password = capability.input("password")
    assert password is not None and password.sensitive
    assert password.example is None
    assert capability.steps[0].params["value"] == "${password}"
    assert "secret_sauce" not in capability.model_dump_json()


def test_a_declined_value_stays_a_literal() -> None:
    """Declining leaves the value hardcoded as part of the flow."""
    step = _executed(
        Action(
            type=ActionType.TYPE,
            locator=Locator.role_name("textbox", "Zip/Postal Code"),
            value="12345",
        ),
        url_before=SAUCEDEMO,
        url_after=SAUCEDEMO,
    )
    capability = Compiler(_AlwaysNo()).compile(_recording([step]))
    assert capability.steps[0].params["value"] == "12345"
    assert capability.inputs == []


def test_parameterizing_is_the_proposed_default() -> None:
    """The compiler proposes; a human declines. Silence keeps the proposal."""
    step = _executed(
        Action(
            type=ActionType.TYPE,
            locator=Locator.role_name("textbox", "Username"),
            value="standard_user",
        ),
        url_before=SAUCEDEMO,
        url_after=SAUCEDEMO,
    )
    capability = _compile(_recording([step]))
    assert capability.steps[0].params["value"] == "${username}"
    assert capability.input("username").example == "standard_user"


def test_a_product_id_in_a_css_fallback_is_templated() -> None:
    """The saucedemo-shaped rule: the item is in the id, not in a value."""
    step = _executed(
        Action(
            type=ActionType.CLICK,
            locator=Locator(
                strategy=LocatorStrategy.ROLE_NAME,
                primary={"role": "button", "name": "Add to cart"},
                fallbacks=[
                    {"strategy": "css", "value": "#add-to-cart-sauce-labs-backpack"}
                ],
            ),
            risk_level=RiskLevel.REVERSIBLE,
        ),
        url_before=f"{SAUCEDEMO}/inventory.html",
        url_after=f"{SAUCEDEMO}/inventory.html",
        nodes=[{"role": "link", "name": "Sauce Labs Backpack"}],
    )
    capability = Compiler(_AlwaysYes()).compile(_recording([step]))

    fallback = capability.steps[0].locator.tiers[1]
    assert fallback.value == "#add-to-cart-${item_name|slug}"
    item = capability.input("item_name")
    assert item is not None and item.example == "Sauce Labs Backpack"


def test_templating_needs_the_label_to_be_visible_on_the_page() -> None:
    """Grounded in the snapshot, not in un-slugging a guess."""
    step = _executed(
        Action(
            type=ActionType.CLICK,
            locator=Locator(
                strategy=LocatorStrategy.ROLE_NAME,
                primary={"role": "button", "name": "Add to cart"},
                fallbacks=[{"strategy": "css", "value": "#add-to-cart-something-else"}],
            ),
        ),
        url_before=f"{SAUCEDEMO}/inventory.html",
        url_after=f"{SAUCEDEMO}/inventory.html",
        nodes=[{"role": "link", "name": "Sauce Labs Backpack"}],
    )
    capability = Compiler(_AlwaysYes()).compile(_recording([step]))
    assert capability.steps[0].locator.tiers[1].value == "#add-to-cart-something-else"
    assert capability.inputs == []


def test_a_checkpoint_is_derived_only_where_the_page_actually_moved() -> None:
    """An assertion that passes whatever happens is worse than none."""
    navigated = _executed(
        Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Login")),
        url_before=SAUCEDEMO,
        url_after=f"{SAUCEDEMO}/inventory.html",
    )
    stayed = _executed(
        Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Add to cart")),
        url_before=f"{SAUCEDEMO}/inventory.html",
        url_after=f"{SAUCEDEMO}/inventory.html",
    )
    capability = _compile(_recording([navigated, stayed]))

    assert capability.steps[0].checkpoint == Checkpoint(
        type=CheckpointType.URL_CONTAINS, value="/inventory.html"
    )
    assert capability.steps[1].checkpoint is None


def test_the_success_checkpoint_is_where_the_run_ended() -> None:
    step = _executed(
        Action(type=ActionType.CLICK, locator=Locator.role_name("button", "Continue")),
        url_before=f"{SAUCEDEMO}/checkout-step-one.html",
        url_after=f"{SAUCEDEMO}/checkout-step-two.html",
    )
    capability = _compile(_recording([step]))
    assert capability.success_checkpoint.value == "/checkout-step-two.html"


def test_an_extract_step_becomes_a_declared_output() -> None:
    step = _executed(
        Action(
            type=ActionType.EXTRACT,
            locator=Locator(strategy=LocatorStrategy.TEXT, primary={"value": "Total:"}),
        ),
        url_before=f"{SAUCEDEMO}/checkout-step-two.html",
        url_after=f"{SAUCEDEMO}/checkout-step-two.html",
        output_name="order_total",
    )
    capability = _compile(_recording([step]))

    assert capability.outputs == [
        OutputSpec(name="order_total", source_step="extract_order_total")
    ]


def test_secrets_are_scrubbed_from_the_artifact_prose_as_well() -> None:
    """The goal text is free prose nobody thinks of as a credential store."""
    step = _executed(
        Action(
            type=ActionType.TYPE,
            locator=Locator.role_name("textbox", "Password"),
            value="secret_sauce",
        ),
        url_before=SAUCEDEMO,
        url_after=SAUCEDEMO,
    )
    recording = _recording([step], goal="log in as standard_user/secret_sauce and check out")

    capability = _compile(recording)

    serialised = capability.model_dump_json()
    assert "secret_sauce" not in serialised
    assert "[REDACTED]" in capability.description
    assert "secret" not in capability.capability_id


class _AlwaysYes:
    """A prompter that accepts every proposal, for tests of the yes path."""

    def confirm(self, question: str, *, default: bool = False) -> bool:
        return True

    def ask(self, question: str, *, default: str = "") -> str:
        return default


class _AlwaysNo:
    """A prompter that declines every proposal."""

    def confirm(self, question: str, *, default: bool = False) -> bool:
        return False

    def ask(self, question: str, *, default: str = "") -> str:
        return default
