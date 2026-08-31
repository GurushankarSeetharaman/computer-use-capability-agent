"""Tests for the evidence trail and redaction.

The redaction tests cut both ways on purpose. A redactor that misses a
secret writes a credential to disk; a redactor that is too eager fills the
log with [REDACTED] and destroys the evidence a failure investigation
depends on. Both are failures, so both are asserted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from computer_use.agent import DiscoveryAgent, DiscoveryOutcome
from computer_use.evidence import EvidenceLog
from computer_use.evidence.redaction import REDACTED, Redactor, looks_sensitive
from computer_use.surface.models import ActionResult, SurfaceSnapshot

PASSWORD = "hunter2-not-a-real-password"


# -- the redactor ----------------------------------------------------------


def test_a_known_literal_is_removed_wherever_it_appears() -> None:
    redactor = Redactor({PASSWORD})
    text = f"typed {PASSWORD} into the field; goal was 'log in with {PASSWORD}'"
    assert PASSWORD not in redactor.redact(text)
    assert redactor.redact(text).count(REDACTED) == 2


def test_a_secret_learned_mid_run_is_honoured() -> None:
    """Discovery does not know the credential until the model types it."""
    redactor = Redactor()
    assert PASSWORD in redactor.redact(PASSWORD)
    redactor.add(PASSWORD)
    assert PASSWORD not in redactor.redact(PASSWORD)


def test_longer_secrets_are_removed_before_shorter_ones() -> None:
    """Otherwise a shared prefix leaves the tail of the longer value behind."""
    redactor = Redactor({"abc123", "abc123456789"})
    assert "456789" not in redactor.redact("abc123456789")


@pytest.mark.parametrize(
    "text",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
        "AKIAIOSFODNN7EXAMPLE",
        "4111 1111 1111 1111",
        "123-45-6789",
        "someone@example.com",
    ],
    ids=["api_key", "bearer", "aws_key", "card", "ssn", "email"],
)
def test_obvious_credentials_are_caught_without_being_declared(text: str) -> None:
    assert REDACTED in Redactor().redact(text)


@pytest.mark.parametrize(
    "text",
    [
        "clicked button 'Add to cart'",
        "url https://www.saucedemo.com/checkout-step-two.html",
        "Total: $32.39",
        "step 12 of 25 took 431 ms",
    ],
)
def test_ordinary_evidence_survives_redaction(text: str) -> None:
    """Over-redaction destroys the record a failure investigation needs."""
    assert Redactor().redact(text) == text


def test_sensitive_field_names_are_recognised() -> None:
    assert looks_sensitive("Password")
    assert looks_sensitive("API Key")
    assert not looks_sensitive("Zip/Postal Code")
    assert not looks_sensitive(None)


# -- the log ---------------------------------------------------------------


def test_the_log_is_one_json_object_per_line(tmp_path: Path) -> None:
    """JSONL so a run that dies partway still leaves a readable log."""
    log = EvidenceLog("run_1", root=tmp_path)
    log.write("step", step_id="a")
    log.write("step", step_id="b")

    lines = log.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["step_id"] for line in lines] == ["a", "b"]


def test_every_record_carries_run_id_timestamp_and_event(tmp_path: Path) -> None:
    log = EvidenceLog("run_1", root=tmp_path)
    record = log.write("step", step_id="a")
    assert {"run_id", "timestamp", "event"} <= record.keys()


def test_a_screenshots_directory_exists_for_every_run(tmp_path: Path) -> None:
    log = EvidenceLog("run_1", root=tmp_path)
    assert log.screenshots.is_dir()


def test_a_secret_nested_inside_a_structure_is_still_removed(tmp_path: Path) -> None:
    """Redaction is applied to the serialised line, not to known fields."""
    log = EvidenceLog("run_1", root=tmp_path, secrets={PASSWORD})
    log.write("step", detail={"attempts": [{"typed": PASSWORD}]})
    assert PASSWORD not in log.path.read_text(encoding="utf-8")


# -- a whole discovery run -------------------------------------------------


@dataclass
class _ToolUse:
    name: str
    input: dict
    id: str = "t1"
    type: str = "tool_use"


@dataclass
class _Response:
    content: list
    stop_reason: str = "tool_use"


class _Client:
    def __init__(self, script):
        self.messages = _Messages(script)


class _Messages:
    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


@dataclass
class _Surface:
    screenshot_dir: Any = None
    actions: list = field(default_factory=list)

    def perceive(self, *, screenshot: bool = True) -> SurfaceSnapshot:
        return SurfaceSnapshot(url="https://www.saucedemo.com/", title="Swag Labs")

    def act(self, action):
        self.actions.append(action)
        return ActionResult(
            action_type=action.type,
            succeeded=True,
            url="https://www.saucedemo.com/",
            locator_tier_label="primary",
        )


def _discovery(tmp_path: Path, goal: str):
    script = [
        _Response(
            [_ToolUse(name="type", input={"role": "textbox", "name": "Password",
                                          "value": PASSWORD})]
        ),
        _Response([_ToolUse(name="done", input={"summary": "ok", "outputs": {}})]),
    ]
    surface = _Surface()
    agent = DiscoveryAgent(surface, client=_Client(script), evidence_root=str(tmp_path))
    return agent.run(goal, "https://www.saucedemo.com")


def test_a_discovery_run_writes_a_log_and_a_recording(tmp_path: Path) -> None:
    result = _discovery(tmp_path, "add an item to the cart")

    assert result.outcome is DiscoveryOutcome.GOAL_MET
    directory = Path(result.evidence_dir)
    assert (directory / "log.jsonl").is_file()
    assert (directory / "recording.json").is_file()
    assert (directory / "screenshots").is_dir()


def test_the_discovery_log_records_actor_action_and_locator_tier(tmp_path: Path) -> None:
    result = _discovery(tmp_path, "add an item to the cart")

    events = EvidenceLog(result.recording.run_id, root=tmp_path).read()
    steps = [e for e in events if e["event"] == "step"]
    assert steps
    assert steps[0]["actor"] == "automation"
    assert steps[0]["action"] == "type"
    assert steps[0]["locator_tier"] == "primary"


def test_the_persisted_recording_holds_no_password(tmp_path: Path) -> None:
    """Section 6: a sensitive value is never persisted in plaintext.

    The in-memory transcript keeps the literal so the compiler can still
    recognise the field as a credential; the file on disk does not.
    """
    result = _discovery(tmp_path, "add an item to the cart")

    recording = (Path(result.evidence_dir) / "recording.json").read_text(encoding="utf-8")
    assert PASSWORD not in recording
    assert REDACTED in recording


def test_no_password_appears_in_any_evidence_file(tmp_path: Path) -> None:
    """The check Prompt 8 asks for, over the whole evidence directory.

    Scoped to a goal that does not itself quote the credential -- see the
    xfail below for the case that does, which is an open question rather
    than a passing behaviour.
    """
    _discovery(tmp_path, "log in and add an item to the cart")

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8"), path


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN QUESTION: a credential written into the operator's goal text reaches "
        "log.jsonl before the redactor can learn it. The redactor only learns a secret "
        "when the model types it into a field that looks sensitive, which happens "
        "several events after discovery_started is written. Resolving this is a design "
        "decision about whether credentials belong in goal text at all -- see the "
        "handover note. Deliberately left failing rather than silently accepted."
    ),
)
def test_a_credential_quoted_in_the_goal_does_not_reach_the_log(tmp_path: Path) -> None:
    _discovery(tmp_path, f"log in as standard_user/{PASSWORD} and add an item")

    log = (tmp_path).rglob("log.jsonl")
    for path in log:
        assert PASSWORD not in path.read_text(encoding="utf-8")
