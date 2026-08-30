"""Command-line entry point for both execution modes.

Two commands, mirroring the two halves of the system:

    python -m computer_use discover --goal "..." --target "..." --out artifacts/
    python -m computer_use replay --artifact artifacts/<id>.json --input k=v

Skeleton only at this stage; wired up in a later build step.
"""


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to discovery or replay.

    Raises NotImplementedError deliberately rather than returning a
    success code, so that an accidental early invocation fails loudly
    instead of exiting 0 and looking like it worked.
    """
    raise NotImplementedError(
        "CLI is not wired up yet; see docs/CLAUDE_CODE_PROMPTS.md prompt 9."
    )
