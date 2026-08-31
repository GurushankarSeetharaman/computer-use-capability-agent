"""The small, closed set of hiccups replay will retry through.

Case 2 of design notes section 3: a checkpoint missed for a reason that is
neither a business outcome nor a real defect -- an interstitial that can be
dismissed, a spinner that has not finished. These get a bounded number of
retries and a log line saying so.

Kept deliberately small and *closed*. A recovery list that grows every time
someone sees a flake stops being error handling and becomes a way to make
failures invisible: the third retry that finally works is indistinguishable
from a step that never had a problem. Anything not on this list is a
failure, and a failure is supposed to be noisy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from computer_use.surface.models import Action, ActionType, Locator, SurfaceSnapshot

#: Roles a modal interstitial announces itself with.
_DIALOG_ROLES = frozenset({"dialog", "alertdialog"})

#: Buttons that make an interstitial go away without agreeing to anything.
#: "Accept" is deliberately absent -- dismissing a dialog is recovery,
#: consenting to whatever it asked is a decision the automation cannot make.
_DISMISS_NAMES = ("close", "dismiss", "ok", "got it", "no thanks")

#: Words a page uses while it is still working.
_BUSY_WORDS = ("loading", "please wait", "processing", "one moment")

_BUSY_ROLES = frozenset({"progressbar"})


class RecoveryPattern(Protocol):
    """Something recognisable that a retry might get past."""

    name: str

    def matches(self, snapshot: SurfaceSnapshot) -> bool: ...

    def remedy(self, snapshot: SurfaceSnapshot) -> Action | None:
        """An action to take before retrying, if any."""


@dataclass(frozen=True)
class DismissibleInterstitial:
    """A modal in the way, with an obvious way to close it."""

    name: str = "dismissible_interstitial"

    def matches(self, snapshot: SurfaceSnapshot) -> bool:
        return any(node.role in _DIALOG_ROLES for node in snapshot.nodes) and (
            self._dismiss_button(snapshot) is not None
        )

    def remedy(self, snapshot: SurfaceSnapshot) -> Action | None:
        button = self._dismiss_button(snapshot)
        if button is None:
            return None
        return Action(
            type=ActionType.CLICK,
            locator=Locator.role_name(button.role, button.name),
            timeout_ms=3000,
        )

    @staticmethod
    def _dismiss_button(snapshot: SurfaceSnapshot):
        for node in snapshot.nodes:
            if node.role != "button" or not node.name:
                continue
            if node.name.strip().lower() in _DISMISS_NAMES:
                return node
        return None


@dataclass(frozen=True)
class TransientBusyState:
    """The page is still working; the remedy is to wait and look again."""

    name: str = "transient_busy_state"

    def matches(self, snapshot: SurfaceSnapshot) -> bool:
        if any(node.role in _BUSY_ROLES for node in snapshot.nodes):
            return True
        text = " ".join(
            part.lower()
            for node in snapshot.nodes
            for part in (node.name, node.value)
            if part
        )
        return any(word in text for word in _BUSY_WORDS)

    def remedy(self, snapshot: SurfaceSnapshot) -> Action | None:
        return None


#: Evaluated in order; the first match wins.
BUILT_IN_PATTERNS: tuple[RecoveryPattern, ...] = (
    DismissibleInterstitial(),
    TransientBusyState(),
)


def find_pattern(
    snapshot: SurfaceSnapshot, patterns: tuple[RecoveryPattern, ...] = BUILT_IN_PATTERNS
) -> RecoveryPattern | None:
    return next((pattern for pattern in patterns if pattern.matches(snapshot)), None)
