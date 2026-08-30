"""Run one real discovery pass against saucedemo. Needs ANTHROPIC_API_KEY.

    set ANTHROPIC_API_KEY=sk-ant-...
    python scripts/scratch_discovery_run.py
    python scripts/scratch_discovery_run.py --headed --max-steps 30

This is the manual end-to-end check for the discovery loop, not a test:
it spends real tokens and drives a real browser, so it is run on purpose
rather than in a suite. The recording is written to disk so the compiler
has something to consume in the next build step.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer_use.agent import DEFAULT_MODEL, DiscoveryAgent, StepOutcome  # noqa: E402
from computer_use.agent.client import build_client, explain_api_error  # noqa: E402
from computer_use.env import (  # noqa: E402
    describe_api_key,
    describe_workspace_id,
    has_api_key,
    load_environment,
)
from computer_use.guardrail import AllowlistConfig  # noqa: E402
from computer_use.surface import PlaywrightSurface  # noqa: E402

TARGET = "https://www.saucedemo.com"
GOAL = (
    "log in as standard_user/secret_sauce and add the Sauce Labs Backpack to the cart, "
    "then reach the checkout overview page"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("evidence/_scratch/recording.json"),
        help="where to write the raw recording",
    )
    args = parser.parse_args()

    loaded = load_environment()
    print(f"env file : {loaded or 'none found (using exported variables)'}")
    print(f"api key  : {describe_api_key()}")
    print(f"workspace: {describe_workspace_id()}")
    if not has_api_key():
        print(
            "\nThe discovery loop needs an API key. Put ANTHROPIC_API_KEY in the "
            ".env file at the repo root, or export it.",
            file=sys.stderr,
        )
        return 2

    allowlist = AllowlistConfig.from_file(Path("config/allowlist.saucedemo.json"))

    with PlaywrightSurface(
        allowlist=allowlist,
        headless=not args.headed,
        screenshot_dir=Path("evidence/_scratch/screenshots"),
    ) as surface:
        # Getting to the target is the operator's job, not the model's: the
        # goal describes what to accomplish on an app, not how to reach it.
        surface.page.goto(TARGET, wait_until="domcontentloaded")

        agent = DiscoveryAgent(
            surface,
            client=build_client(),
            model=args.model,
            max_steps=args.max_steps,
        )
        try:
            result = agent.run(args.goal, TARGET)
        except Exception as error:  # surfaced, not swallowed
            hint = explain_api_error(error)
            if hint is None:
                raise
            print(f"\n{error}\n\n{hint}", file=sys.stderr)
            return 2

    print(f"\noutcome : {result.outcome.value}")
    print(f"summary : {result.summary or '-'}")
    print(f"outputs : {result.outputs or {}}")
    print(f"\nsteps ({len(result.recording.steps)}):")
    for step in result.recording.steps:
        marker = {
            StepOutcome.EXECUTED: "ok  ",
            StepOutcome.BLOCKED: "BLOCK",
            StepOutcome.FAILED: "FAIL",
            StepOutcome.INVALID_TOOL_CALL: "BAD ",
            StepOutcome.CONTROL: "ctrl",
        }[step.outcome]
        tier = step.result.locator_tier_label if step.result else None
        detail = step.action.describe() if step.action else (step.tool_name or "-")
        print(f"  [{marker}] {detail}" + (f"  <{tier}>" if tier else ""))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(result.recording.model_dump_json(indent=2), encoding="utf-8")
    print(f"\nrecording written to {args.out}")
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
