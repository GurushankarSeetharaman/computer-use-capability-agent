"""Command-line entry point for both execution modes.

    python -m computer_use discover --goal "..." --target "https://..." --out artifacts/
    python -m computer_use replay --artifact artifacts/<id>.json --input name=value

The two commands are deliberately asymmetric in what they cost. `discover`
spends model tokens and produces an artifact; `replay` spends nothing and
consumes one. That asymmetry is the whole point of the system, so the CLI
does not try to hide it behind a single verb.

On secrets: values passed with --input land in shell history and in the
process list, where anyone on the machine can read them. That is fine for
saucedemo's published test credentials and wrong for anything real, so
both commands also accept --input-env / --credential-env, which name an
environment variable to read instead. The recommended form for a real
credential is the env one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from computer_use.env import describe_api_key, has_api_key, load_environment

DEFAULT_ALLOWLIST = Path("config/allowlist.saucedemo.json")
DEFAULT_ARTIFACT_DIR = Path("artifacts")


# -- argument helpers ------------------------------------------------------


def _pair(text: str) -> tuple[str, str]:
    """name=value."""
    if "=" not in text:
        raise argparse.ArgumentTypeError(f"expected name=value, got {text!r}")
    name, value = text.split("=", 1)
    return name.strip(), value


def _env_pair(text: str) -> tuple[str, str]:
    """name=ENV_VAR, or bare name meaning the upper-cased name."""
    if "=" in text:
        name, variable = text.split("=", 1)
        return name.strip(), variable.strip()
    name = text.strip()
    return name, name.upper()


def resolve_values(
    literals: list[tuple[str, str]], from_env: list[tuple[str, str]]
) -> dict[str, str]:
    """Combine literal values with ones read from the environment.

    Missing environment variables are an error rather than an empty
    string: silently supplying "" for a password produces a confusing
    business outcome several steps later instead of an obvious complaint
    here.
    """
    values = dict(literals)
    missing = []
    for name, variable in from_env:
        raw = os.environ.get(variable)
        if raw is None:
            missing.append(f"{name} <- ${variable}")
            continue
        values[name] = raw
    if missing:
        raise SystemExit(
            "these environment variables are not set: " + ", ".join(missing)
        )
    return values


def _require_api_key() -> None:
    if not has_api_key():
        raise SystemExit(
            "discover needs an Anthropic API key. Put ANTHROPIC_API_KEY in the .env "
            "file at the repository root, or export it. (replay needs no key.)"
        )


# -- discover --------------------------------------------------------------


def discover(args: argparse.Namespace) -> int:
    """Drive the goal with the model, then compile what worked."""
    from computer_use.agent import DiscoveryAgent
    from computer_use.agent.client import build_client, explain_api_error
    from computer_use.artifact import CliPrompter, Compiler, NonInteractivePrompter, save
    from computer_use.guardrail import AllowlistConfig
    from computer_use.surface import PlaywrightSurface

    _require_api_key()
    credentials = resolve_values(args.credential, args.credential_env)

    print(f"goal      : {args.goal}")
    print(f"target    : {args.target}")
    print(f"api key   : {describe_api_key()}")
    if credentials:
        # Names only. The values are not printed here and are never shown
        # to the model either.
        print(f"credentials: {', '.join(sorted(credentials))} (values withheld)")

    with PlaywrightSurface(
        allowlist=AllowlistConfig.from_file(args.allowlist),
        headless=not args.headed,
    ) as surface:
        surface.page.goto(args.target, wait_until="domcontentloaded")
        agent = DiscoveryAgent(
            surface,
            client=build_client(),
            model=args.model,
            max_steps=args.max_steps,
            verbose=args.verbose,
        )
        try:
            result = agent.run(args.goal, args.target, credentials=credentials)
        except Exception as error:
            hint = explain_api_error(error)
            if hint is None:
                raise
            print(f"\n{error}\n\n{hint}", file=sys.stderr)
            return 2

    print(f"\noutcome  : {result.outcome.value}")
    print(f"evidence : {result.evidence_dir}")

    if not result.succeeded:
        # Compiling a flow that did not finish would produce a capability
        # that half-works, and the failure would surface at replay against
        # whatever the caller was relying on it for.
        print(
            "\nnot compiling: the run did not reach its goal. The recording is in the "
            "evidence directory above if you want to inspect what happened.",
            file=sys.stderr,
        )
        return 1

    prompter = NonInteractivePrompter() if args.non_interactive else CliPrompter()
    capability = Compiler(prompter).compile(result.recording, capability_id=args.capability_id)
    path = save(capability, args.out)

    print(f"\ncapability: {capability.capability_id}")
    print(f"inputs    : " + ", ".join(
        f"{spec.name}{' (sensitive)' if spec.sensitive else ''}" for spec in capability.inputs
    ))
    print(f"outputs   : " + ", ".join(spec.name for spec in capability.outputs))
    print(f"written to {path}")
    return 0


# -- replay ----------------------------------------------------------------


def replay(args: argparse.Namespace) -> int:
    """Execute a capability deterministically. No model, no API key."""
    from computer_use.artifact import load
    from computer_use.escalation import EscalationManager, RunState
    from computer_use.guardrail import AllowlistConfig
    from computer_use.replay import InputValidationError, ReplayEngine
    from computer_use.surface import PlaywrightSurface

    capability = load(args.artifact)
    values = resolve_values(args.input, args.input_env)

    print(f"capability: {capability.capability_id} v{capability.version}")
    print(f"inputs    : {', '.join(sorted(values)) or '(none)'}")

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
        remote_debugging_port=args.remote_debugging_port,
    ) as surface:
        surface.page.goto(capability.target.base_url, wait_until="domcontentloaded")
        engine = ReplayEngine(
            surface,
            escalation=manager,
            force_escalate_at_step=args.force_escalate_at_step,
            verbose=args.verbose,
        )
        try:
            result = engine.run(capability, values)
        except InputValidationError as error:
            print(f"\ninputs do not satisfy this capability's contract:\n{error}", file=sys.stderr)
            return 2

    print("\n" + json.dumps(result.to_contract(), indent=2))
    print(f"\nevidence: evidence/{result.run_id}/")
    # Exit code distinguishes the three statuses: 0 success, 3 business
    # outcome, 1 failure. A business outcome is not a failure, and a
    # caller scripting this should not have to parse stdout to tell.
    if result.succeeded:
        return 0
    return 3 if result.status.value == "business_outcome" else 1


# -- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m computer_use",
        description="Discover a browser capability once; replay it deterministically.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    common.add_argument("--headed", action="store_true", help="show the browser window")
    common.add_argument(
        "--verbose", action="store_true", help="screenshot every step, not just failures"
    )

    d = commands.add_parser(
        "discover", parents=[common], help="drive a goal with the model and compile it"
    )
    d.add_argument("--goal", required=True)
    d.add_argument("--target", required=True)
    d.add_argument("--out", type=Path, default=DEFAULT_ARTIFACT_DIR)
    d.add_argument("--capability-id", default=None)
    d.add_argument("--model", default=None)
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument(
        "--credential",
        action="append",
        type=_pair,
        default=[],
        metavar="name=value",
        help="a credential the model may use by placeholder but never sees",
    )
    d.add_argument(
        "--credential-env",
        action="append",
        type=_env_pair,
        default=[],
        metavar="name[=ENV_VAR]",
        help="read a credential from the environment (recommended for real secrets)",
    )
    d.add_argument(
        "--non-interactive",
        action="store_true",
        help="accept the compiler's proposals instead of being asked",
    )
    d.set_defaults(handler=discover)

    r = commands.add_parser(
        "replay", parents=[common], help="execute a capability artifact, no model involved"
    )
    r.add_argument("--artifact", type=Path, required=True)
    r.add_argument(
        "--input", action="append", type=_pair, default=[], metavar="name=value"
    )
    r.add_argument(
        "--input-env",
        action="append",
        type=_env_pair,
        default=[],
        metavar="name[=ENV_VAR]",
        help="read an input from the environment, keeping it out of shell history "
        "and the process list (recommended for real secrets)",
    )
    r.add_argument("--force-escalate-at-step", default=None, metavar="STEP_ID")
    r.add_argument("--remote-debugging-port", type=int, default=None)
    r.add_argument("--escalation-timeout", type=float, default=300.0)
    r.add_argument("--console-port", type=int, default=8765)
    r.set_defaults(handler=replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to discovery or replay."""
    load_environment()
    args = build_parser().parse_args(argv)
    if getattr(args, "model", None) is None and args.command == "discover":
        from computer_use.agent import DEFAULT_MODEL

        args.model = DEFAULT_MODEL
    return args.handler(args)
