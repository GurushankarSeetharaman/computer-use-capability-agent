"""Keeping secrets out of files, on the way in.

Two layers, and both are needed for different reasons.

**Known literals.** The values of inputs the artifact marks `sensitive`.
This is the precise layer: we know exactly what the secret is, so we can
remove every occurrence of it wherever it appears -- including places
nobody thinks of as a credential store, like a goal string that happens to
read "log in as standard_user/hunter2".

**Patterns.** A small set of regexes for things that are obviously secret
even when nobody declared them: API keys, card numbers, government id
numbers, email addresses. This layer exists because the first one only
works for values the schema knew about, and a run that types a card number
into an undeclared field would otherwise write it to disk in full.

Patterns are deliberately conservative. A redactor that is too eager
produces logs full of [REDACTED] and quietly destroys the evidence a
failure investigation depends on -- which is its own kind of failure. When
in doubt, the literal layer is the one that should be catching it, by the
input being declared sensitive in the artifact.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

#: Ordered, and each one is here because leaving it out would be
#: indefensible rather than because it might occasionally match.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Anthropic and OpenAI style keys: a fixed prefix plus a long token.
    ("api_key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")),
    # Bearer tokens in a header or a log line.
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE)),
    # AWS access key ids.
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # 13-19 digit card-like runs, optionally separated. Bounded on both
    # sides so a long numeric id in a URL is not mistaken for one.
    ("card_number", re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")),
    # US social security numbers.
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
)


class Redactor:
    """Removes known secrets and obvious credentials from text."""

    def __init__(self, secrets: set[str] | None = None) -> None:
        #: Sorted longest-first so that removing a long secret cannot be
        #: pre-empted by a shorter one that happens to be a substring of it,
        #: which would leave the tail of the longer value in the file.
        self._secrets = sorted(
            (s for s in (secrets or set()) if s and len(s) >= 3), key=len, reverse=True
        )

    def add(self, secret: str | None) -> None:
        """Learn a secret mid-run, e.g. when a credential field is filled."""
        if secret and len(secret) >= 3 and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: str) -> str:
        return self.redact(text)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for _, pattern in PATTERNS:
            text = pattern.sub(REDACTED, text)
        return text

    @property
    def secrets(self) -> list[str]:
        return list(self._secrets)


#: Field names whose values are secrets. Shared with the compiler so that
#: what discovery redacts and what compilation parameterizes cannot drift
#: apart -- a field treated as sensitive in one and not the other is the
#: exact gap through which a password reaches a file.
SENSITIVE_FIELD_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api key",
    "apikey",
    "credential",
    "pin",
    "ssn",
    "social security",
    "card number",
    "cvv",
)


def looks_sensitive(field_name: str | None) -> bool:
    lowered = (field_name or "").lower()
    return any(hint in lowered for hint in SENSITIVE_FIELD_HINTS)
