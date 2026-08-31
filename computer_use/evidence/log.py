"""Structured, redacted run logs.

One JSONL file per run, one line per step. JSONL rather than a single JSON
document because a run that dies partway through still leaves a readable
log -- which is exactly the run whose log you need.

Redaction happens on the way *in*, never as a later pass. A log file that
briefly contained a password before being cleaned is a log file that
contained a password: it was on disk, it may have been read, and on some
filesystems the old bytes are still there. Values are redacted before
serialisation reaches the file handle.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from computer_use.evidence.redaction import REDACTED, Redactor

EVIDENCE_ROOT = Path("evidence")


class EvidenceLog:
    """Append-only JSONL log for one run."""

    def __init__(
        self,
        run_id: str,
        *,
        root: Path | str = EVIDENCE_ROOT,
        secrets: set[str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.directory = Path(root) / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "log.jsonl"
        self.screenshots = self.directory / "screenshots"
        self.screenshots.mkdir(parents=True, exist_ok=True)
        #: Known literals plus pattern matching. Held in memory only, for
        #: the life of the run.
        self.redactor = Redactor(secrets)

    def learn_secret(self, value: str | None) -> None:
        """Register a secret discovered mid-run, before anything is written.

        Discovery does not know what the credentials are until the model
        types them, so the redactor has to be able to grow. Called before
        the step that used the value is logged, never after.
        """
        self.redactor.add(value)

    def redact(self, text: str) -> str:
        return self.redactor.redact(text)

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event. Returns what was written, already redacted."""
        record = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        # Serialise first, then redact the text: a secret can hide in a
        # nested structure, and scrubbing the flat JSON catches every one
        # of them without walking arbitrary shapes.
        line = self.redact(json.dumps(record, default=str))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return json.loads(line)

    def read(self) -> list[dict[str, Any]]:
        """Every event written so far, for tests and for the CLI summary."""
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
