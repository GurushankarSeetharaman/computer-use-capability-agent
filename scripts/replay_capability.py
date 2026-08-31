"""Replay a capability artifact against the live target. No model involved.

    python scripts/replay_capability.py --input username=standard_user ...
    python scripts/replay_capability.py --headed --force-escalate-at-step click_login

The escalation flag exists so the handoff can be demonstrated without
waiting for something to break: it pauses at a named step, prints the
attach URL and the operator console, and waits for a human to hand control
back. Pair it with --headed and --remote-debugging-port to actually drive
the paused session.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer_use.artifact import load  # noqa: E402
from computer_use.escalation import EscalationManager, RunState  # noqa: E402
from computer_use.guardrail import AllowlistConfig  # noqa: E402
from computer_use.replay import ReplayEngine  # noqa: E402
from computer_use.surface import PlaywrightSurface  # noqa: E402

DEFAULT_ARTIFACT = Path("artifacts/examples/saucedemo_add_item_to_cart_checkout.json")
DEFAULT_ALLOWLIST = Path("config/allowlist.saucedemo.json")


def parse_input(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise argparse.ArgumentTypeError(f"expected name=value, got {pair!r}")
    name, value = pair.split("=", 1)
    return name.strip(), value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--input", action="append", type=parse_input, default=[], metavar="name=value"
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--force-escalate-at-step",
        default=None,
        metavar="STEP_ID",
        help="pause and hand off at this step, to demonstrate control transfer",
    )
    parser.add_argument(
        "--remote-debugging-port",
        type=int,
        default=None,
        help="expose a DevTools endpoint so a human can attach to the live session",
    )
    parser.add_argument("--escalation-timeout", type=float, default=300.0)
    parser.add_argument("--console-port", type=int, default=8765)
    args = parser.parse_args()

    capability = load(args.artifact)
    values = dict(args.input)

    print(f"capability : {capability.capability_id} v{capability.version}")
    print(f"inputs     : {', '.join(sorted(values)) or '(none)'}")

    manager = None
    if args.force_escalate_at_step:
        manager = EscalationManager(
            RunState(capability.capability_id),
            console_port=args.console_port,
            timeout_s=args.escalation_timeout,
        )

    with PlaywrightSurface(
        allowlist=AllowlistConfig.from_file(args.allowlist),
        headless=not args.headed,
        screenshot_dir=Path("evidence/_replay"),
        remote_debugging_port=args.remote_debugging_port,
    ) as surface:
        surface.page.goto(capability.target.base_url, wait_until="domcontentloaded")
        engine = ReplayEngine(
            surface,
            escalation=manager,
            force_escalate_at_step=args.force_escalate_at_step,
        )
        result = engine.run(capability, values)

    print("\n" + json.dumps(result.to_contract(), indent=2))
    print(f"\nevidence: evidence/{result.run_id}/log.jsonl")
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
