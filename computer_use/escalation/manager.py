"""Pausing a run, handing the live session to a human, and taking it back.

The mechanism, not the console, is the point (design notes section 5).
Three properties matter and each is enforced here rather than assumed:

**The browser context is never closed.** An "escalation" that tears down
the session and asks a human to start over is a restart. This module only
ever flips a flag and waits; nothing in it touches the surface's
lifecycle.

**Control ownership is explicit and atomic.** `control_owner` flips to
human before the operator is told the session is theirs, and back only
when they hand it over. Both transitions are written to the evidence log,
so "who was driving at 14:07" is answerable after the fact.

**What the human did is recorded in the same trail.** Captured at
navigation granularity by polling the page while paused -- see the note on
`_pump` for what that does and does not cover.
"""

from __future__ import annotations

import time
from typing import Any

from computer_use.escalation.console import OperatorConsole
from computer_use.escalation.models import (
    ControlOwner,
    EscalationOutcome,
    InterventionRequest,
    RunState,
)
from computer_use.evidence.log import EvidenceLog

#: How long a paused run waits for a human before giving up.
#:
#: A run that blocks forever is worse than one that stops: unattended
#: automation would hang a queue indefinitely, and nobody watching a
#: dashboard can tell "waiting for a person" from "wedged". Timing out
#: back to the caller's normal failure path keeps the run's outcome
#: honest -- the work did not get done, and that is reportable.
DEFAULT_TIMEOUT_S = 300.0

#: How often the paused loop looks at the page. Also the granularity at
#: which human navigation is captured.
_POLL_INTERVAL_S = 0.5


class EscalationManager:
    """Owns the pause/handoff/resume cycle for one run."""

    def __init__(
        self,
        state: RunState,
        *,
        log: EvidenceLog | None = None,
        console_port: int = 8765,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        serve_console: bool = True,
        auto_resume: bool = False,
    ) -> None:
        self.state = state
        self.log = log
        self.console_port = console_port
        self.timeout_s = timeout_s
        self.serve_console = serve_console
        #: Resume immediately without waiting for a human. For tests and
        #: for demonstrating the mechanism without an operator present.
        self.auto_resume = auto_resume
        self.requests: list[InterventionRequest] = []

    # -- the handoff -------------------------------------------------------

    def pause(
        self, request: InterventionRequest, *, surface: Any | None = None
    ) -> EscalationOutcome:
        """Hand the live session to a human and wait for it back.

        The surface is passed so its page can be observed and its CDP
        endpoint advertised. It is never closed, and no action is taken on
        it -- while a human is in control the automation must not be
        touching the page they are driving.
        """
        self.requests.append(request)
        self.state.hand_to_human()
        self._record("escalation_requested", request.model_dump(mode="json"))
        self._record("control_transferred", {"to": ControlOwner.HUMAN.value})

        cdp_url = getattr(surface, "cdp_url", None) if surface is not None else None
        console = OperatorConsole(self.state, port=self.console_port)
        console_url = console.start(request, cdp_url=cdp_url) if self.serve_console else None

        self._announce(request, console_url, cdp_url)
        try:
            outcome = self._wait(surface)
        finally:
            console.stop()

        self.state.hand_to_automation()
        self._record(
            "control_transferred",
            {"to": ControlOwner.AUTOMATION.value, "outcome": outcome.value},
        )
        return outcome

    def _wait(self, surface: Any | None) -> EscalationOutcome:
        if self.auto_resume:
            self.state.hand_to_automation()
            return EscalationOutcome.RESUMED

        deadline = time.monotonic() + self.timeout_s
        last_url = self._url_of(surface)
        while time.monotonic() < deadline:
            if self.state.resumed.wait(_POLL_INTERVAL_S):
                return EscalationOutcome.RESUMED
            last_url = self._pump(surface, last_url)
        return EscalationOutcome.TIMED_OUT

    def _pump(self, surface: Any | None, last_url: str | None) -> str | None:
        """Observe the page while a human drives it.

        Navigation granularity, by polling. This records where the human
        went, not every click and keystroke: capturing those would mean
        instrumenting the page through CDP, and a half-built input trace
        that silently misses events would be worse evidence than an honest
        record of the pages visited. Named as a limitation rather than
        papered over.
        """
        current = self._url_of(surface)
        if current is not None and current != last_url:
            self._record(
                "human_action",
                {"actor": "human", "action": "navigated", "url": current},
            )
        return current

    @staticmethod
    def _url_of(surface: Any | None) -> str | None:
        if surface is None:
            return None
        try:
            return surface.page.url
        except Exception:
            return None

    # -- reporting ---------------------------------------------------------

    def _announce(
        self, request: InterventionRequest, console_url: str | None, cdp_url: str | None
    ) -> None:
        print("\n" + request.describe())
        if cdp_url:
            print(f"  attach : {cdp_url}")
        else:
            print("  attach : (start the surface with remote_debugging_port to attach)")
        if console_url:
            print(f"  resume : {console_url}")
        print(f"  waiting up to {self.timeout_s:.0f}s for a human to take over\n")

    def _record(self, event: str, fields: dict[str, Any]) -> None:
        if self.log is not None:
            self.log.write(event, **fields)


def request_intervention(
    request: InterventionRequest, manager: EscalationManager | None = None, **kwargs: Any
) -> InterventionRequest:
    """Report a handoff request, pausing for a human when one can be reached.

    Kept as a function because both the discovery loop and the replay
    engine call it, and neither should have to know whether an operator
    console is configured for this run.
    """
    if manager is None:
        print(request.describe())
        return request
    manager.pause(request, **kwargs)
    return request
