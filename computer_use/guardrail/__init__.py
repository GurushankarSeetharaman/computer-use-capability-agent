"""Guardrail: allowlist + risk check, enforced before every action.

Pure policy. No Playwright, no Anthropic SDK, no I/O beyond loading its
own config -- which is what makes it exhaustively unit-testable, and why
it is built before the components it protects.

Why one enforcement point instead of two: discovery actions come from a
model and replay actions come from a file, but both reach the page only
through act(), and both must pass this check first. Two checks would
eventually disagree; the interesting failure mode is the path that
forgets to call one. There is only one path (design notes section 6).

The three-way verdict matters as much as the check. Allow, deny, and
needs-escalation are different outcomes with different owners: deny stops
the run, escalation hands it to a person. Collapsing them into a boolean
would force a choice between blocking legitimate work and letting
irreversible actions through unattended.
"""

from computer_use.guardrail.models import (
    AllowlistConfig,
    CheckResult,
    Decision,
    GuardrailViolation,
    origin_of,
    path_of,
)
from computer_use.guardrail.policy import Guardrail

__all__ = [
    "AllowlistConfig",
    "CheckResult",
    "Decision",
    "Guardrail",
    "GuardrailViolation",
    "origin_of",
    "path_of",
]
