"""Surface adapter: the only code that knows what a browser is.

Exposes two verbs -- perceive() returns a pruned accessibility-tree
snapshot plus a screenshot, act(action) executes one action -- and both
discovery and replay drive the world exclusively through them.

Why isolate it: this is the seam that swaps for a legacy-web or desktop
adapter later (design notes section 4). A desktop adapter would trade
Playwright for an OS accessibility API but expose the *same* role+name
locator shape, so artifacts recorded against this adapter survive the
swap. That is only true if nothing outside this package ever touches
Playwright directly.

PlaywrightSurface is imported lazily so that the models and locator logic
stay importable -- and testable -- in an environment with no browser
installed. Importing the name pulls Playwright in at that moment.
"""

from typing import Any

from computer_use.surface.locators import (
    Resolution,
    ResolutionReport,
    build_handle,
    describe_tiers,
    resolve,
)
from computer_use.surface.models import (
    A11yNode,
    Action,
    ActionResult,
    ActionType,
    BoundingBox,
    Locator,
    LocatorSpec,
    LocatorStrategy,
    RiskLevel,
    SurfaceSnapshot,
    TierAttempt,
    TierOutcome,
    WaitCondition,
    tier_label,
)

__all__ = [
    "A11yNode",
    "Action",
    "ActionResult",
    "ActionType",
    "BoundingBox",
    "Locator",
    "LocatorNotFound",
    "LocatorSpec",
    "LocatorStrategy",
    "PlaywrightSurface",
    "Resolution",
    "ResolutionReport",
    "RiskLevel",
    "SurfaceSnapshot",
    "TierAttempt",
    "TierOutcome",
    "WaitCondition",
    "build_handle",
    "describe_tiers",
    "resolve",
    "tier_label",
]

_LAZY = {"PlaywrightSurface", "LocatorNotFound", "SurfaceNotStarted"}


def __getattr__(name: str) -> Any:
    """Defer the Playwright-dependent imports until they are actually used."""
    if name in _LAZY:
        from computer_use.surface import adapter

        return getattr(adapter, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
