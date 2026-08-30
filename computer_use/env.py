"""Loading local configuration, and reporting on it without disclosing it.

Two rules shape this module.

**Loading happens at entry points, never on import.** Importing a library
should not mutate the process environment -- a test that imports the agent
package would otherwise silently acquire whatever is in a developer's .env,
and a real key could leak into a run nobody meant to bill. Scripts and the
CLI call load_environment() explicitly; nothing else does.

**A real environment variable outranks the file.** load_dotenv is called
with override=False so that an exported ANTHROPIC_API_KEY -- from CI, a
secrets manager, or a deliberate one-off -- is not silently replaced by a
stale value in a local file.

Nothing here ever returns or logs a secret's value. describe_api_key()
reports presence and length only, which is enough to tell "unset" from
"set but truncated" without putting the key on a terminal, in a log file,
or in an evidence artifact.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

#: The repository root, two levels up from this file.
REPO_ROOT = Path(__file__).resolve().parent.parent

API_KEY_VAR = "ANTHROPIC_API_KEY"


def load_environment(path: Path | str | None = None, *, override: bool = False) -> Path | None:
    """Load a .env file into the process environment.

    Returns the file that was loaded, or None if there was none -- absence
    is normal, not an error: the key may already be exported.
    """
    dotenv_path = Path(path) if path is not None else REPO_ROOT / ".env"
    if not dotenv_path.is_file():
        return None
    load_dotenv(dotenv_path, override=override)
    return dotenv_path


def has_api_key() -> bool:
    """Whether an Anthropic API key is present in the environment."""
    return bool(os.environ.get(API_KEY_VAR, "").strip())


def describe_api_key() -> str:
    """Presence and length only -- never the value, not even a prefix.

    Length is deliberately the most that is disclosed. It distinguishes
    "unset" from "set but truncated by a stray newline", which is the one
    diagnostic that actually comes up, while revealing nothing usable.
    """
    value = os.environ.get(API_KEY_VAR, "")
    if not value.strip():
        return f"{API_KEY_VAR}: not set"
    return f"{API_KEY_VAR}: set ({len(value)} characters)"


def require_api_key() -> str:
    """Return the key, or raise with an actionable message.

    The message names the variable and the file, and quotes neither.
    """
    value = os.environ.get(API_KEY_VAR, "").strip()
    if not value:
        raise RuntimeError(
            f"{API_KEY_VAR} is not set. Put it in {REPO_ROOT / '.env'} as "
            f"{API_KEY_VAR}=... or export it in your shell."
        )
    return value
