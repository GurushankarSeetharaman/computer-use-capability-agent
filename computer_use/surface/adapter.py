"""The Playwright-backed surface adapter: perceive() and act().

This is the only module in the project that imports Playwright. Everything
above it -- the agent loop, the replay engine, the guardrail -- speaks in
Actions and Snapshots, which is what makes the desktop/legacy-web adapter
in design notes section 4 a substitution rather than a rewrite.

The adapter is intentionally unopinionated about outcomes. It reports what
happened and which locator tier got there; deciding whether a given
outcome is a business result, a transient hiccup, or a hard failure is the
replay engine's job, because that judgement needs artifact context this
layer does not have.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from computer_use.guardrail import AllowlistConfig, Guardrail
from computer_use.surface.aria import AriaEntry, parse_aria_snapshot
from computer_use.surface.locators import ResolutionReport, resolve
from computer_use.surface.models import (
    A11yNode,
    Action,
    ActionResult,
    ActionType,
    BoundingBox,
    SurfaceSnapshot,
    WaitCondition,
    tier_label,
)

#: Roles that represent something a user can operate. Always kept when
#: pruning, named or not -- an unnamed control is exactly the kind of thing
#: a human operator needs to see in the snapshot to understand a failure.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
    }
)

#: Roles kept only when they carry a name. These are not operable, but they
#: are how a page says what happened -- an error banner, a heading, a status
#: line. Without them a snapshot can show a login form that looks identical
#: before and after a rejected password.
INFORMATIVE_ROLES = frozenset(
    {"alert", "heading", "status", "img", "list", "listitem", "paragraph", "text"}
)

#: Roles that Playwright's get_by_role accepts, used when attaching bounding
#: boxes. The accessibility tree exposes engine-internal roles too, and
#: passing one of those to get_by_role raises rather than returning nothing.
ADDRESSABLE_ROLES = INTERACTIVE_ROLES | {"alert", "heading", "img", "status"}


class LocatorNotFound(RuntimeError):
    """No tier of a locator identified exactly one element.

    Carries the report so callers can log which tiers were *ambiguous*
    rather than merely absent. The two say different things about how the
    target application changed, and only one of them means "your locator
    is now underspecified".
    """

    def __init__(self, message: str, report: ResolutionReport) -> None:
        super().__init__(message)
        self.report = report


class SurfaceNotStarted(RuntimeError):
    """perceive()/act() called before the browser was started."""


class PlaywrightSurface:
    """A live browser page, addressed through perceive() and act().

    A surface is bound to an allowlist at construction and enforces it on
    every act(). perceive() is deliberately not gated: looking at a page
    changes nothing, and a policy that could block observation would blind
    the evidence trail exactly when a run is going wrong.

    Use as a context manager. The browser context is exposed deliberately:
    escalation hands a human the *same* context rather than opening a new
    one (design notes section 5), so nothing here may close it implicitly.
    Teardown is only ever explicit, via close() or leaving the with-block.
    """

    def __init__(
        self,
        *,
        allowlist: AllowlistConfig,
        headless: bool = True,
        screenshot_dir: Path | str = Path("evidence/_surface"),
        probe_timeout_ms: int = 2000,
        capture_bounding_boxes: bool = True,
        max_nodes: int = 200,
        remote_debugging_port: int | None = None,
    ) -> None:
        #: Required, with no default. There is no way to construct a surface
        #: without a policy, which is the point: design notes section 6 asks
        #: for one enforcement point rather than two, and a check every
        #: caller is merely supposed to remember is not an enforcement point.
        self.allowlist = allowlist
        self.headless = headless
        self.screenshot_dir = Path(screenshot_dir)
        self.probe_timeout_ms = probe_timeout_ms
        self.capture_bounding_boxes = capture_bounding_boxes
        self.max_nodes = max_nodes
        #: When set, Chromium is launched with a DevTools endpoint so a
        #: human can attach to the *same* session during an escalation
        #: (design notes section 5). Off by default: an open debugging
        #: port is a control channel into the browser, and it should exist
        #: only when someone means to use it.
        self.remote_debugging_port = remote_debugging_port

        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._screenshot_counter = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> PlaywrightSurface:
        """Launch a browser and open one page."""
        self._playwright = sync_playwright().start()
        args = (
            [f"--remote-debugging-port={self.remote_debugging_port}"]
            if self.remote_debugging_port
            else []
        )
        self._browser = self._playwright.chromium.launch(headless=self.headless, args=args)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        return self

    def close(self) -> None:
        """Tear everything down. Never called implicitly on escalation."""
        for closer in (self._context, self._browser):
            if closer is not None:
                closer.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None

    def __enter__(self) -> PlaywrightSurface:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise SurfaceNotStarted("call start() or use the surface as a context manager")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """The live browser context -- the thing handed to a human on handoff."""
        if self._context is None:
            raise SurfaceNotStarted("call start() or use the surface as a context manager")
        return self._context

    @property
    def cdp_url(self) -> str | None:
        """Where a human can attach to this exact browser, if enabled.

        None rather than an empty string when no port was configured, so a
        caller can tell "attaching is not available for this run" from "the
        endpoint is here" without parsing a URL.
        """
        if not self.remote_debugging_port:
            return None
        return f"http://127.0.0.1:{self.remote_debugging_port}"

    # -- perceive ----------------------------------------------------------

    def perceive(self, *, screenshot: bool = True) -> SurfaceSnapshot:
        """Capture what is on screen: pruned accessibility tree + screenshot.

        The tree comes from the browser's own accessibility computation
        rather than a hand-rolled DOM walk, because the names it produces
        are the same ones get_by_role matches against. A snapshot whose
        names did not agree with the locators would quietly generate
        unresolvable actions.
        """
        page = self.page
        path = self._capture_screenshot() if screenshot else None
        nodes = self._prune(parse_aria_snapshot(page.aria_snapshot()))
        if self.capture_bounding_boxes:
            nodes = [self._attach_bounding_box(node) for node in nodes]
        return SurfaceSnapshot(
            url=page.url,
            title=page.title(),
            nodes=nodes,
            screenshot_path=str(path) if path else None,
        )

    def _prune(self, entries: list[AriaEntry]) -> list[A11yNode]:
        """Keep interactive and informative nodes; drop structural scaffolding.

        Informative roles are kept when they carry a name *or* a value,
        because that is how a page reports what just happened: saucedemo's
        rejected-login banner arrives as a node whose text is its value,
        and a rule that demanded a name would drop the single most
        important element on the page.

        Capped at max_nodes: a snapshot is an LLM input, and an unbounded
        one turns a verbose page into a context-window failure.
        """
        collected: list[A11yNode] = []
        for entry in entries:
            if len(collected) >= self.max_nodes:
                break
            informative = entry.role in INFORMATIVE_ROLES and (entry.name or entry.value)
            if entry.role in INTERACTIVE_ROLES or informative:
                collected.append(
                    A11yNode(role=entry.role, name=entry.name, value=entry.value)
                )
        return collected

    def _attach_bounding_box(self, node: A11yNode) -> A11yNode:
        """Best-effort screen position for a node.

        Resolved through the same role+name locator the agent would use to
        act on it, which gives the coordinates a second job: a node that
        cannot be located this way comes back without a box, flagging an
        element the automation can see but not reliably address.
        """
        if node.role not in ADDRESSABLE_ROLES or not node.name:
            return node
        try:
            handle = self.page.get_by_role(node.role, name=node.name)
            if handle.count() != 1:
                return node
            box = handle.bounding_box()
        except Exception:
            return node
        if not box:
            return node
        return node.model_copy(update={"bounding_box": BoundingBox(**box)})

    def _capture_screenshot(self) -> Path:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_counter += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = self.screenshot_dir / f"{stamp}-{self._screenshot_counter:03d}.png"
        self.page.screenshot(path=str(path))
        return path

    # -- act ---------------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        """Check one action against policy, then execute it if permitted.

        The guardrail runs here, inside the only method that can reach the
        page, so no caller can skip it -- discovery and replay get the same
        enforcement without either of them being trusted to ask for it
        (design notes section 6). A refused action returns a result flagged
        ``blocked`` and never touches the browser.

        Execution failures also come back as an unsuccessful ActionResult
        rather than an exception; see the ActionResult docstring for why
        that judgement belongs to the caller and not to this layer.
        """
        started = time.monotonic()

        verdict = Guardrail.check(
            action, self.allowlist, current_url=self._current_url()
        )
        if not verdict.allowed:
            return ActionResult.blocked_by_guardrail(
                action,
                reason=verdict.reason,
                needs_escalation=verdict.needs_escalation,
                url=verdict.checked_url,
                duration_ms=self._elapsed_ms(started),
            )

        try:
            report, extracted = self._perform(action)
        except LocatorNotFound as exc:
            # Keeps the whole ladder, so an ambiguous tier reaches the
            # evidence log even when nothing resolved at all.
            return ActionResult.failure(
                action,
                error=str(exc),
                duration_ms=self._elapsed_ms(started),
                url=self._current_url(),
                locator_attempts=exc.report.attempts,
                tiers_attempted=len(exc.report.attempts),
            )
        except Exception as exc:
            return ActionResult.failure(
                action,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=self._elapsed_ms(started),
                url=self._current_url(),
            )

        resolution = report.resolution if report else None
        attempts = report.attempts if report else []
        return ActionResult(
            action_type=action.type,
            succeeded=True,
            locator_tier=resolution.tier if resolution else None,
            locator_tier_label=tier_label(resolution.tier) if resolution else None,
            locator_strategy=resolution.spec.strategy if resolution else None,
            tiers_attempted=len(attempts),
            locator_attempts=attempts,
            extracted=extracted,
            url=self._current_url(),
            duration_ms=self._elapsed_ms(started),
        )

    def _perform(self, action: Action) -> tuple[ResolutionReport | None, str | None]:
        """Dispatch one action. Returns (resolution report, extracted text)."""
        if action.type is ActionType.NAVIGATE:
            self.page.goto(
                action.url, timeout=action.timeout_ms, wait_until="domcontentloaded"
            )
            return None, None
        if action.type is ActionType.WAIT_FOR:
            return self._wait_for(action), None

        report = self._require(action)
        handle = report.resolution.handle
        if action.type is ActionType.CLICK:
            handle.click(timeout=action.timeout_ms)
            return report, None
        if action.type is ActionType.TYPE:
            # fill() rather than type(): it clears first and sets the value in
            # one step, so a replayed step cannot append to a field that a
            # previous attempt left populated.
            handle.fill(action.value, timeout=action.timeout_ms)
            return report, None
        if action.type is ActionType.SELECT:
            handle.select_option(action.value, timeout=action.timeout_ms)
            return report, None
        if action.type is ActionType.EXTRACT:
            return report, self._read(handle, action.timeout_ms)
        raise ValueError(f"unsupported action type: {action.type}")

    def _require(self, action: Action) -> ResolutionReport:
        """Resolve an action's locator, or fail describing every tier tried.

        The message reports each tier's outcome rather than just listing
        the tiers, so an underspecified locator reads as "matched 3
        elements" instead of a generic miss. That text is also what the
        discovery agent sees, which is what lets it narrow the locator on
        the next turn instead of retrying the same thing.
        """
        report = resolve(self.page, action.locator, probe_timeout_ms=self.probe_timeout_ms)
        if not report.resolved:
            raise LocatorNotFound(
                f"no tier uniquely matched for {action.type.value}: {report.describe()}",
                report,
            )
        return report

    def _read(self, handle: Any, timeout_ms: int) -> str:
        """Read a value from a form control, or text from anything else.

        Tried in that order because input_value() is the only one that sees
        what a user typed; inner_text() on a filled input returns nothing.
        """
        try:
            return handle.input_value(timeout=timeout_ms)
        except Exception:
            return handle.inner_text(timeout=timeout_ms)

    def _wait_for(self, action: Action) -> ResolutionReport | None:
        """Wait on a bounded condition. Never a bare sleep."""
        if action.condition is WaitCondition.VISIBLE:
            report = self._require(action)
            report.resolution.handle.wait_for(state="visible", timeout=action.timeout_ms)
            return report
        if action.condition is WaitCondition.TEXT_PRESENT:
            self.page.get_by_text(action.value).first.wait_for(
                state="visible", timeout=action.timeout_ms
            )
            return None
        if action.condition is WaitCondition.URL_CONTAINS:
            target = action.value
            self.page.wait_for_url(
                lambda url: target in url, timeout=action.timeout_ms
            )
            return None
        raise ValueError(f"unsupported wait condition: {action.condition}")

    # -- small helpers -----------------------------------------------------

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _current_url(self) -> str | None:
        """URL after the action, tolerating a page that has gone away."""
        try:
            return self.page.url
        except Exception:
            return None
