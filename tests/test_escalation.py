"""Tests for pause, control transfer and resume.

The property under test throughout is the one design notes section 5 calls
load-bearing: the session survives the handoff. Nothing here closes a
surface, and the tests assert that the automation stops touching the page
while a human owns it -- because an escalation that keeps clicking is not
a handoff, it is a race.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from computer_use.escalation import (
    ControlOwner,
    EscalationManager,
    EscalationOutcome,
    InterventionRequest,
    OperatorConsole,
    RunState,
)
from computer_use.evidence import EvidenceLog


def _request(**overrides) -> InterventionRequest:
    base = dict(
        run_id="run_1",
        capability_or_goal="add an item to the cart",
        current_step="click_login",
        reason="checkpoint did not pass",
        url="https://www.saucedemo.com/",
    )
    base.update(overrides)
    return InterventionRequest(**base)


class FakeSurface:
    """A surface that records whether anyone touched it."""

    def __init__(self, urls: list[str] | None = None) -> None:
        self._urls = urls or ["https://www.saucedemo.com/"]
        self.cdp_url = "http://127.0.0.1:9222"
        self.closed = False
        self.acted = 0

    @property
    def page(self):
        surface = self

        class _Page:
            @property
            def url(self) -> str:
                # Advances as a human navigates, one look at a time.
                return surface._urls[0] if len(surface._urls) == 1 else surface._urls.pop(0)

        return _Page()

    def close(self) -> None:
        self.closed = True


# -- run state -------------------------------------------------------------


def test_control_starts_with_the_automation() -> None:
    state = RunState("run_1")
    assert state.control_owner is ControlOwner.AUTOMATION
    assert not state.human_in_control


def test_control_flips_both_ways() -> None:
    state = RunState("run_1")
    assert state.hand_to_human() is ControlOwner.HUMAN
    assert state.human_in_control
    assert state.hand_to_automation() is ControlOwner.AUTOMATION
    assert not state.human_in_control


def test_handing_back_wakes_whatever_is_waiting() -> None:
    """The paused loop blocks on this event; a resume has to release it."""
    state = RunState("run_1")
    state.hand_to_human()
    assert not state.resumed.is_set()

    threading.Timer(0.05, state.hand_to_automation).start()

    assert state.resumed.wait(2.0), "resume did not wake the waiter"
    assert state.control_owner is ControlOwner.AUTOMATION


def test_state_snapshot_answers_who_is_in_control() -> None:
    state = RunState("run_1")
    state.hand_to_human()
    assert state.snapshot() == {"run_id": "run_1", "control_owner": "human"}


# -- the request -----------------------------------------------------------


def test_the_request_carries_what_an_operator_needs() -> None:
    described = _request().describe()
    for expected in ("run_1", "click_login", "checkpoint did not pass", "saucedemo"):
        assert expected in described


# -- pause and resume ------------------------------------------------------


def test_pause_hands_control_over_and_takes_it_back(tmp_path: Path) -> None:
    state = RunState("run_1")
    log = EvidenceLog("run_1", root=tmp_path)
    manager = EscalationManager(state, log=log, serve_console=False, auto_resume=True)

    outcome = manager.pause(_request(), surface=FakeSurface())

    assert outcome is EscalationOutcome.RESUMED
    assert state.control_owner is ControlOwner.AUTOMATION, "control must come back"


def test_the_surface_is_never_closed_by_a_handoff(tmp_path: Path) -> None:
    """A handoff that tears down the session is a restart, not a handoff."""
    surface = FakeSurface()
    manager = EscalationManager(
        RunState("run_1"),
        log=EvidenceLog("run_1", root=tmp_path),
        serve_console=False,
        auto_resume=True,
    )

    manager.pause(_request(), surface=surface)

    assert not surface.closed
    assert surface.acted == 0, "the automation must not act while a human is driving"


def test_both_control_transfers_are_written_to_the_evidence_log(tmp_path: Path) -> None:
    """"Who was driving at 14:07" has to be answerable after the fact."""
    log = EvidenceLog("run_1", root=tmp_path)
    manager = EscalationManager(
        RunState("run_1"), log=log, serve_console=False, auto_resume=True
    )

    manager.pause(_request(), surface=FakeSurface())

    events = log.read()
    transfers = [e for e in events if e["event"] == "control_transferred"]
    assert [t["to"] for t in transfers] == ["human", "automation"]
    assert any(e["event"] == "escalation_requested" for e in events)


def test_a_run_nobody_takes_over_times_out(tmp_path: Path) -> None:
    """Blocking forever would hang a queue and look identical to wedged."""
    manager = EscalationManager(
        RunState("run_1"),
        log=EvidenceLog("run_1", root=tmp_path),
        serve_console=False,
        timeout_s=0.2,
    )

    outcome = manager.pause(_request(), surface=FakeSurface())

    assert outcome is EscalationOutcome.TIMED_OUT
    assert manager.state.control_owner is ControlOwner.AUTOMATION


def test_human_navigation_is_captured_tagged_as_human(tmp_path: Path) -> None:
    """Navigation granularity: what the human did, in the same trail."""
    log = EvidenceLog("run_1", root=tmp_path)
    surface = FakeSurface(
        urls=[
            "https://www.saucedemo.com/",
            "https://www.saucedemo.com/inventory.html",
            "https://www.saucedemo.com/cart.html",
        ]
    )
    manager = EscalationManager(
        RunState("run_1"), log=log, serve_console=False, timeout_s=1.5
    )

    manager.pause(_request(), surface=surface)

    human = [e for e in log.read() if e["event"] == "human_action"]
    assert human, "a human navigating must leave a trace"
    assert all(e["actor"] == "human" for e in human)
    assert any("/inventory.html" in e["url"] for e in human)


# -- operator console ------------------------------------------------------


@pytest.fixture
def console():
    state = RunState("run_1")
    console = OperatorConsole(state, port=8791)
    console.start(_request(), cdp_url="http://127.0.0.1:9222")
    yield console
    console.stop()


def test_the_console_reports_who_is_in_control(console: OperatorConsole) -> None:
    with urllib.request.urlopen(f"{console.url}state", timeout=5) as response:
        assert json.load(response) == {"run_id": "run_1", "control_owner": "automation"}


def test_the_console_page_shows_the_reason_and_the_attach_url(
    console: OperatorConsole,
) -> None:
    with urllib.request.urlopen(console.url, timeout=5) as response:
        page = response.read().decode()
    assert "checkpoint did not pass" in page
    assert "127.0.0.1:9222" in page
    assert "Resume automation" in page


def test_the_resume_button_flips_control_back(console: OperatorConsole) -> None:
    console.state.hand_to_human()
    assert console.state.human_in_control

    urllib.request.urlopen(
        urllib.request.Request(f"{console.url}resume", method="POST"), timeout=5
    )

    assert console.state.control_owner is ControlOwner.AUTOMATION
    assert console.state.resumed.is_set()


# -- surface attachment ----------------------------------------------------


def test_a_surface_advertises_no_attach_url_unless_asked() -> None:
    """An open debugging port is a control channel; it is opt-in."""
    from computer_use.guardrail import AllowlistConfig
    from computer_use.surface.adapter import PlaywrightSurface

    allowlist = AllowlistConfig(
        allowed_base_urls=["https://www.saucedemo.com"],
        allowed_action_types=["click"],
    )
    assert PlaywrightSurface(allowlist=allowlist).cdp_url is None
    assert (
        PlaywrightSurface(allowlist=allowlist, remote_debugging_port=9222).cdp_url
        == "http://127.0.0.1:9222"
    )
