"""Capability artifact: the schema, and the compiler that produces one.

The artifact is the deliverable of a discovery run -- a typed, versioned
JSON document describing how to accomplish one goal, with named `inputs`
and `outputs` so a caller reads it as a function signature rather than as
a step list (design notes section 2).

Why the compiler is a separate pass rather than a mode of the agent loop:
the artifact must be decoupled from the raw model transcript. A separate
pass takes the recording as input and emits only the successful path, so
the distillation rules live in one readable place and can be reasoned
about -- and tested -- without running a browser or a model.

Validation lives in the schema, not in replay. An artifact whose output
names a step that does not exist, or whose step references an input the
caller cannot supply, fails when it loads rather than partway through a
flow that may already have done something irreversible.
"""

from computer_use.artifact.compiler import (
    CliPrompter,
    Compiler,
    NonInteractivePrompter,
    Prompter,
)
from computer_use.artifact.models import (
    ApprovalState,
    BusinessOutcome,
    Capability,
    Checkpoint,
    CheckpointType,
    DiscoveredBy,
    InputSpec,
    MatchType,
    OutcomeMatch,
    OutputSpec,
    Provenance,
    Step,
    SurfaceType,
    Target,
    ValueType,
)
from computer_use.artifact.store import ARTIFACTS_DIR, load, save
from computer_use.artifact.templating import placeholders, render, slugify

__all__ = [
    "ARTIFACTS_DIR",
    "ApprovalState",
    "BusinessOutcome",
    "Capability",
    "Checkpoint",
    "CheckpointType",
    "CliPrompter",
    "Compiler",
    "DiscoveredBy",
    "InputSpec",
    "MatchType",
    "NonInteractivePrompter",
    "OutcomeMatch",
    "OutputSpec",
    "Prompter",
    "Provenance",
    "Step",
    "SurfaceType",
    "Target",
    "ValueType",
    "load",
    "placeholders",
    "render",
    "save",
    "slugify",
]
