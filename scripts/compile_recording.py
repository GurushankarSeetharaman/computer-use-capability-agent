"""Compile a raw recording into a capability artifact.

    python scripts/compile_recording.py                      # interactive
    python scripts/compile_recording.py --non-interactive    # accept defaults

Interactive by default: which literals become typed inputs is a judgement
call the compiler deliberately does not make on its own.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from computer_use.agent.recording import Recording  # noqa: E402
from computer_use.artifact import (  # noqa: E402
    CliPrompter,
    Compiler,
    NonInteractivePrompter,
    load,
    save,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recording", type=Path, default=Path("evidence/_scratch/recording.json")
    )
    parser.add_argument("--out", type=Path, default=Path("artifacts"))
    parser.add_argument("--capability-id", default=None)
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="accept every default instead of asking",
    )
    args = parser.parse_args()

    recording = Recording.model_validate_json(args.recording.read_text(encoding="utf-8"))
    print(f"recording : {args.recording}")
    print(f"goal      : {recording.goal}")
    print(f"steps     : {len(recording.steps)} recorded, "
          f"{len(recording.successful_steps)} successful\n")

    prompter = NonInteractivePrompter() if args.non_interactive else CliPrompter()
    capability = Compiler(prompter).compile(recording, capability_id=args.capability_id)

    path = save(capability, args.out)
    reloaded = load(path)  # proves it validates from disk, not just in memory

    print(f"\ncapability : {reloaded.capability_id}")
    print(f"inputs     : " + ", ".join(
        f"{i.name}{' (sensitive)' if i.sensitive else ''}" for i in reloaded.inputs
    ) or "(none)")
    print(f"outputs    : " + ", ".join(
        f"{o.name} <- {o.source_step}" for o in reloaded.outputs
    ) or "(none)")
    print(f"steps      : {len(reloaded.steps)}")
    for step in reloaded.steps:
        checkpoint = step.checkpoint.value if step.checkpoint else "-"
        print(f"  {step.step_id:28} {step.action.value:9} risk={step.risk_level.value:12} "
              f"checkpoint={checkpoint}")
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
