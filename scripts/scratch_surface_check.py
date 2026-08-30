"""Manual smoke check for the surface adapter. Not a test -- run it by hand.

    python scripts/scratch_surface_check.py            # headless
    python scripts/scratch_surface_check.py --headed   # watch it happen

Drives https://www.saucedemo.com far enough to show every moving part of
the adapter working end to end:

  1. perceive() -- pruned accessibility tree + screenshot on disk
  2. act(type)  -- with a deliberately broken primary locator, so the
                   fallback fires and the reported tier proves it
  3. act(click) -- the login button, found by role+name
  4. perceive() -- the page's own error banner, which is what makes this a
                   preview of the business-outcome path in design notes
                   section 3 rather than just a click that returned True

Deliberately submits an incomplete login: the point is to see the adapter
report accurately, and a rejected login gives the page something visible
to say.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer_use.guardrail import AllowlistConfig  # noqa: E402
from computer_use.surface import (  # noqa: E402
    Action,
    ActionType,
    Locator,
    LocatorStrategy,
    PlaywrightSurface,
)

TARGET = "https://www.saucedemo.com"
ALLOWLIST = Path("config/allowlist.saucedemo.json")


def show_snapshot(label: str, snapshot) -> None:
    print(f"\n--- {label} " + "-" * (58 - len(label)))
    print(snapshot.describe())
    print(f"screenshot: {snapshot.screenshot_path}")


def show_result(label: str, result) -> None:
    tier = result.locator_tier_label or "n/a"
    status = "ok" if result.succeeded else "FAILED"
    print(f"\n[{status}] {label}")
    strategy = result.locator_strategy.value if result.locator_strategy else "-"
    print(f"    locator tier : {tier} ({strategy})")
    print(f"    tiers tried  : {result.tiers_attempted}")
    print(f"    duration     : {result.duration_ms} ms")
    if result.extracted is not None:
        print(f"    extracted    : {result.extracted!r}")
    if result.error:
        print(f"    error        : {result.error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    with PlaywrightSurface(
        allowlist=AllowlistConfig.from_file(ALLOWLIST),
        headless=not args.headed,
        screenshot_dir=Path("evidence/_scratch"),
    ) as surface:
        show_result(
            "navigate to saucedemo",
            surface.act(Action(type=ActionType.NAVIGATE, url=TARGET, timeout_ms=15000)),
        )
        show_snapshot("perceive: login page", surface.perceive())

        # A locator whose primary cannot match, to prove the fallback path
        # reports honestly rather than quietly succeeding as "primary".
        drifted = Locator(
            strategy=LocatorStrategy.ROLE_NAME,
            primary={"role": "textbox", "name": "This Name Does Not Exist"},
            fallbacks=[{"strategy": "css", "value": "#user-name"}],
        )
        show_result(
            "type into username via a drifted locator (expect fallback_1)",
            surface.act(
                Action(type=ActionType.TYPE, locator=drifted, value="standard_user")
            ),
        )

        show_result(
            "click Login by role+name (expect primary)",
            surface.act(
                Action(
                    type=ActionType.CLICK,
                    locator=Locator.role_name("button", "Login", css_fallback="#login-button"),
                )
            ),
        )

        show_snapshot("perceive: after submitting without a password", surface.perceive())

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
