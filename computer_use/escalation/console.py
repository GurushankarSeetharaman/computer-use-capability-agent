"""A bare operator console: one page, one Resume button.

Deliberately minimal, and design notes section 7 says so explicitly -- the
console is mocked, the control-transfer mechanism is real. What is being
demonstrated is that a human can take a live session, drive it, and hand
it back mid-flow; a nicer button would demonstrate nothing further.

Built on http.server from the standard library for the same reason: adding
a web framework to this project would buy nothing and would put a
dependency between an operator and their ability to unblock a stuck run.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from computer_use.escalation.models import InterventionRequest, RunState

_PAGE = """<!doctype html>
<title>Operator console - {run_id}</title>
<style>
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 3rem auto; max-width: 40rem;
          color: #1a1a1a; }}
  .card {{ border: 1px solid #d4d4d4; border-radius: 8px; padding: 1.25rem 1.5rem; }}
  dt {{ color: #666; font-size: 13px; margin-top: .75rem; }}
  dd {{ margin: .15rem 0 0; font-family: ui-monospace, monospace; }}
  .owner {{ display: inline-block; padding: .15rem .5rem; border-radius: 4px;
            background: #fde68a; font-family: ui-monospace, monospace; }}
  button {{ font: inherit; padding: .6rem 1.4rem; border-radius: 6px; border: 0;
            background: #1a1a1a; color: #fff; cursor: pointer; margin-top: 1.5rem; }}
  .done {{ background: #bbf7d0; }}
</style>
<h1>Automation paused</h1>
<div class="card">
  <p>Control is currently with <span class="owner">{owner}</span>.</p>
  <dl>
    <dt>run</dt><dd>{run_id}</dd>
    <dt>step</dt><dd>{step}</dd>
    <dt>reason</dt><dd>{reason}</dd>
    <dt>url</dt><dd>{url}</dd>
    <dt>attach to the live browser</dt><dd>{cdp}</dd>
  </dl>
  <p>The browser session is still open and is the same one the automation was
     driving. Drive it yourself, then hand control back.</p>
  <form method="post" action="/resume"><button>Resume automation</button></form>
</div>
"""

_RESUMED = """<!doctype html>
<title>Resumed</title>
<style>body {{ font: 15px/1.5 system-ui, sans-serif; margin: 3rem auto; max-width: 40rem; }}
.card {{ border: 1px solid #86efac; background: #f0fdf4; border-radius: 8px; padding: 1.5rem; }}</style>
<div class="card"><h1>Control handed back</h1>
<p>The automation has resumed from the current page state. You can close this tab.</p></div>
"""


class OperatorConsole:
    """Serves the console on a background thread for the life of a pause."""

    def __init__(self, state: RunState, *, port: int = 8765) -> None:
        self.state = state
        self.port = port
        self.request: InterventionRequest | None = None
        self.cdp_url: str | None = None
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self, request: InterventionRequest, *, cdp_url: str | None = None) -> str:
        self.request = request
        self.cdp_url = cdp_url
        handler = _make_handler(self)
        # Port 0 would be tidier, but an operator has to be able to find
        # this page from a printed line, so the port stays predictable.
        self._server = HTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _make_handler(console: OperatorConsole) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            """Silence per-request logging; the evidence log is the record."""

        def _send(self, body: str, status: int = 200, content_type: str = "text/html") -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.path.rstrip("/") == "/state":
                self._send(json.dumps(console.state.snapshot()), content_type="application/json")
                return
            request = console.request
            self._send(
                _PAGE.format(
                    run_id=console.state.run_id,
                    owner=console.state.control_owner.value,
                    step=request.current_step if request else "-",
                    reason=request.reason if request else "-",
                    url=request.url if request else "-",
                    cdp=console.cdp_url or "(no remote-debugging port configured)",
                )
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/resume":
                self._send("not found", status=404, content_type="text/plain")
                return
            console.state.hand_to_automation()
            self._send(_RESUMED)

    return Handler
