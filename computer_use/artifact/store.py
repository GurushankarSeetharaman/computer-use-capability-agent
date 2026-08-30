"""Reading and writing capability artifacts.

Validation happens on load, not at first use. A malformed artifact should
fail before a browser is launched -- not four steps into a replay, which
on a flow containing an irreversible action is the worst possible moment
to discover that step 6 references an input nobody declared.
"""

from __future__ import annotations

from pathlib import Path

from computer_use.artifact.models import Capability

ARTIFACTS_DIR = Path("artifacts")


def save(capability: Capability, directory: Path | str = ARTIFACTS_DIR) -> Path:
    """Write one capability to <directory>/<capability_id>.json."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{capability.capability_id}.json"
    path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")
    return path


def load(path: Path | str) -> Capability:
    """Load and fully validate a capability, or raise explaining why not."""
    return Capability.model_validate_json(Path(path).read_text(encoding="utf-8"))
