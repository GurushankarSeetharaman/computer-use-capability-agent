"""Replay engine: execute a capability artifact deterministically.

No LLM is involved. Steps run in order through the same guardrail as
discovery -- enforced inside surface.act(), not re-implemented here -- and
after each one its checkpoint is asserted before advancing.

Why checkpoint mismatches are classified rather than raised: the three
outcomes are genuinely different kinds of thing (design notes section 3).
A business outcome ("invalid credentials") is a legitimate answer the
caller asked for. A recoverable hiccup is noise to retry through. A hard
failure is a defect needing evidence and a human. Collapsing these into
one exception hierarchy with severity levels is the design mistake this
module exists to avoid -- so they are three distinct top-level statuses.

The classification order is not incidental either. A business outcome is
checked first, because retrying a rejected password three times is both
useless and, on a system that locks accounts, harmful.
"""

from computer_use.replay.engine import (
    InputValidationError,
    ReplayEngine,
    input_model,
    validate_inputs,
)
from computer_use.replay.models import ReplayResult, ReplayStatus
from computer_use.replay.recovery import (
    BUILT_IN_PATTERNS,
    DismissibleInterstitial,
    RecoveryPattern,
    TransientBusyState,
    find_pattern,
)

__all__ = [
    "BUILT_IN_PATTERNS",
    "DismissibleInterstitial",
    "InputValidationError",
    "RecoveryPattern",
    "ReplayEngine",
    "ReplayResult",
    "ReplayStatus",
    "TransientBusyState",
    "find_pattern",
    "input_model",
    "validate_inputs",
]
