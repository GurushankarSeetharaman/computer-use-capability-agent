"""Unit tests for computer_use.guardrail -- the policy layer.

This is the code meant to stop the expensive mistakes, so it gets the most
thorough tests in the project. That is affordable precisely because the
guardrail imports neither Playwright nor the Anthropic SDK: every branch
below runs in microseconds against plain objects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from computer_use.guardrail import (
    AllowlistConfig,
    CheckResult,
    Decision,
    Guardrail,
    GuardrailViolation,
    origin_of,
)
from computer_use.surface.models import (
    Action,
    ActionType,
    Locator,
    RiskLevel,
)

SAUCEDEMO = "https://www.saucedemo.com"
INVENTORY = f"{SAUCEDEMO}/inventory.html"


@pytest.fixture
def config() -> AllowlistConfig:
    return AllowlistConfig(
        capability_id="sauce_add_item_to_cart_checkout",
        allowed_base_urls=[SAUCEDEMO],
        allowed_routes=["/", "/inventory.html", "/checkout-step-*.html"],
        allowed_action_types=[
            ActionType.NAVIGATE,
            ActionType.CLICK,
            ActionType.TYPE,
            ActionType.EXTRACT,
            ActionType.WAIT_FOR,
        ],
    )


def click(**kwargs) -> Action:
    """A well-formed click, so each test varies only what it is about."""
    return Action(
        type=ActionType.CLICK,
        locator=Locator.role_name("button", "Login"),
        **kwargs,
    )


# -- the four cases the build prompt calls for -----------------------------


def test_allowed_action_passes(config: AllowlistConfig) -> None:
    result = Guardrail.check(click(), config, current_url=INVENTORY)
    assert result.decision is Decision.ALLOW
    assert result.allowed


def test_disallowed_route_is_blocked(config: AllowlistConfig) -> None:
    result = Guardrail.check(click(), config, current_url=f"{SAUCEDEMO}/admin.html")
    assert result.decision is Decision.DENY
    assert "/admin.html" in result.reason


def test_irreversible_without_approval_routes_to_escalation(
    config: AllowlistConfig,
) -> None:
    """Not a denial: the action is probably right, it just needs an owner."""
    action = click(risk_level=RiskLevel.IRREVERSIBLE)
    result = Guardrail.check(action, config, current_url=INVENTORY)
    assert result.decision is Decision.NEEDS_ESCALATION
    assert result.needs_escalation
    assert not result.allowed


def test_irreversible_with_approval_passes(config: AllowlistConfig) -> None:
    action = click(risk_level=RiskLevel.IRREVERSIBLE, approved=True)
    result = Guardrail.check(action, config, current_url=INVENTORY)
    assert result.decision is Decision.ALLOW


# -- action types ----------------------------------------------------------


def test_action_type_outside_the_allowlist_is_denied(config: AllowlistConfig) -> None:
    action = Action(
        type=ActionType.SELECT,
        locator=Locator.role_name("combobox", "Sort"),
        value="lohi",
    )
    result = Guardrail.check(action, config, current_url=INVENTORY)
    assert result.decision is Decision.DENY
    assert "select" in result.reason


def test_action_type_is_checked_before_the_target(config: AllowlistConfig) -> None:
    """The wrong kind of action is rejected without asking where it points."""
    action = Action(
        type=ActionType.SELECT,
        locator=Locator.role_name("combobox", "Sort"),
        value="lohi",
    )
    result = Guardrail.check(action, config, current_url="https://evil.example/x")
    assert result.decision is Decision.DENY
    assert "action type" in result.reason


# -- origins ---------------------------------------------------------------


def test_navigation_off_the_allowlisted_origin_is_denied(config: AllowlistConfig) -> None:
    action = Action(type=ActionType.NAVIGATE, url="https://evil.example/inventory.html")
    result = Guardrail.check(action, config)
    assert result.decision is Decision.DENY
    assert "not allowlisted" in result.reason


def test_lookalike_host_suffix_cannot_pass_as_the_allowlisted_origin(
    config: AllowlistConfig,
) -> None:
    """The classic allowlist bypass: a prefix test would let this through."""
    action = Action(
        type=ActionType.NAVIGATE, url="https://www.saucedemo.com.attacker.example/"
    )
    result = Guardrail.check(action, config)
    assert result.decision is Decision.DENY


def test_subdomain_of_an_allowlisted_host_is_not_implied(config: AllowlistConfig) -> None:
    action = Action(type=ActionType.NAVIGATE, url="https://staging.saucedemo.com/")
    assert Guardrail.check(action, config).decision is Decision.DENY


def test_scheme_downgrade_is_a_different_origin(config: AllowlistConfig) -> None:
    action = Action(type=ActionType.NAVIGATE, url="http://www.saucedemo.com/")
    assert Guardrail.check(action, config).decision is Decision.DENY


def test_origin_normalises_case_and_default_ports() -> None:
    assert origin_of("https://WWW.SauceDemo.com/x") == "https://www.saucedemo.com"
    assert origin_of("https://www.saucedemo.com:443/x") == "https://www.saucedemo.com"
    assert origin_of("https://www.saucedemo.com:8443/x") == "https://www.saucedemo.com:8443"
    assert origin_of("/inventory.html") is None


# -- targets ---------------------------------------------------------------


def test_action_without_a_known_current_url_is_denied(config: AllowlistConfig) -> None:
    """We cannot show it is in bounds, and "probably fine" is not a policy."""
    result = Guardrail.check(click(), config)
    assert result.decision is Decision.DENY
    assert "no current_url" in result.reason


def test_navigate_supplies_its_own_target(config: AllowlistConfig) -> None:
    """A navigate needs no current_url: its target is the URL it names."""
    action = Action(type=ActionType.NAVIGATE, url=INVENTORY)
    assert Guardrail.check(action, config).decision is Decision.ALLOW


# -- routes ----------------------------------------------------------------


def test_glob_routes_match(config: AllowlistConfig) -> None:
    for path in ("/", "/inventory.html", "/checkout-step-one.html", "/checkout-step-two.html"):
        result = Guardrail.check(click(), config, current_url=f"{SAUCEDEMO}{path}")
        assert result.decision is Decision.ALLOW, path


def test_route_matching_is_case_sensitive_everywhere(config: AllowlistConfig) -> None:
    """fnmatchcase, not fnmatch: a verdict must not depend on the host OS."""
    result = Guardrail.check(click(), config, current_url=f"{SAUCEDEMO}/INVENTORY.HTML")
    assert result.decision is Decision.DENY


def test_empty_route_list_permits_any_path_on_an_allowed_origin() -> None:
    config = AllowlistConfig(
        allowed_base_urls=[SAUCEDEMO],
        allowed_action_types=[ActionType.CLICK],
    )
    result = Guardrail.check(click(), config, current_url=f"{SAUCEDEMO}/anything/at/all")
    assert result.decision is Decision.ALLOW


# -- config loading --------------------------------------------------------


def test_config_requires_at_least_one_base_url_and_action_type() -> None:
    with pytest.raises(ValidationError):
        AllowlistConfig(allowed_base_urls=[], allowed_action_types=[ActionType.CLICK])
    with pytest.raises(ValidationError):
        AllowlistConfig(allowed_base_urls=[SAUCEDEMO], allowed_action_types=[])


def test_config_rejects_a_relative_base_url() -> None:
    with pytest.raises(ValidationError):
        AllowlistConfig(
            allowed_base_urls=["/inventory.html"],
            allowed_action_types=[ActionType.CLICK],
        )


def test_config_rejects_unknown_fields() -> None:
    """A misspelled policy key must fail loudly, not silently permit more."""
    with pytest.raises(ValidationError):
        AllowlistConfig(
            allowed_base_urls=[SAUCEDEMO],
            allowed_action_types=[ActionType.CLICK],
            allowed_domains=["evil.example"],
        )


def test_config_loads_from_a_json_file(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "capability_id": "demo",
                "allowed_base_urls": [SAUCEDEMO],
                "allowed_routes": ["/inventory.html"],
                "allowed_action_types": ["click"],
            }
        ),
        encoding="utf-8",
    )
    config = AllowlistConfig.from_file(path)
    assert config.capability_id == "demo"
    assert config.allowed_origins == {"https://www.saucedemo.com"}


def test_malformed_config_file_fails_at_load_not_at_use(tmp_path: Path) -> None:
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"allowed_base_urls": [SAUCEDEMO]}), encoding="utf-8")
    with pytest.raises(ValidationError):
        AllowlistConfig.from_file(path)


def test_the_committed_saucedemo_config_is_valid() -> None:
    """The example policy ships with the repo; it must actually load."""
    config = AllowlistConfig.from_file(
        Path(__file__).resolve().parent.parent / "config" / "allowlist.saucedemo.json"
    )
    assert config.allowed_origins == {"https://www.saucedemo.com"}
    assert ActionType.CLICK in config.allowed_action_types


# -- result ergonomics -----------------------------------------------------


def test_raise_if_blocked_raises_for_denial_and_escalation(config: AllowlistConfig) -> None:
    denied = Guardrail.check(click(), config, current_url="https://evil.example/")
    with pytest.raises(GuardrailViolation) as denial:
        denied.raise_if_blocked()
    assert denial.value.result.decision is Decision.DENY

    escalating = Guardrail.check(
        click(risk_level=RiskLevel.IRREVERSIBLE), config, current_url=INVENTORY
    )
    with pytest.raises(GuardrailViolation):
        escalating.raise_if_blocked()


def test_raise_if_blocked_returns_the_result_when_allowed(config: AllowlistConfig) -> None:
    result = Guardrail.check(click(), config, current_url=INVENTORY)
    assert result.raise_if_blocked() is result


def test_every_result_explains_itself(config: AllowlistConfig) -> None:
    """A denial nobody can explain is a denial that gets worked around."""
    checks = [
        Guardrail.check(click(), config, current_url=INVENTORY),
        Guardrail.check(click(), config, current_url="https://evil.example/"),
        Guardrail.check(
            click(risk_level=RiskLevel.IRREVERSIBLE), config, current_url=INVENTORY
        ),
    ]
    for result in checks:
        assert isinstance(result, CheckResult)
        assert result.reason.strip()


# -- architectural invariant ----------------------------------------------


def test_guardrail_imports_without_playwright_or_anthropic() -> None:
    """Pure policy: importing it must not drag in a browser or an LLM client.

    Checked in a subprocess because the assertion is about what a fresh
    interpreter loads, and this one has already imported plenty.
    """
    code = (
        "import sys; import computer_use.guardrail; "
        "loaded = [m for m in ('playwright', 'anthropic') if m in sys.modules]; "
        "print(','.join(loaded))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", f"guardrail pulled in {completed.stdout.strip()}"
