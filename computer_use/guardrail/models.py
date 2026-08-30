"""Policy vocabulary: what is permitted, and what a check concluded.

Loaded from JSON rather than YAML so that reading a policy file adds no
dependency. A policy that needs a third-party parser to be inspected is a
policy fewer people will actually read.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from computer_use.surface.models import ActionType, RiskLevel

#: Ports that carry no information once the scheme is known. Normalised away
#: so that https://host and https://host:443 are recognised as one origin.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_of(url: str) -> str | None:
    """Reduce a URL to scheme://host[:port], or None if it is not absolute.

    Origins are compared as parsed structures, never as string prefixes.
    A prefix test would accept https://www.saucedemo.com.attacker.example
    against an allowlist entry of https://www.saucedemo.com, which is the
    classic way a URL allowlist is defeated.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None or _DEFAULT_PORTS.get(scheme) == port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def path_of(url: str) -> str:
    """The path component, defaulting to / so route patterns always match something."""
    return urlparse(url).path or "/"


class AllowlistConfig(BaseModel):
    """What one capability is permitted to touch.

    Scoped per capability rather than globally, because "which URLs may
    this automation visit" is only answerable in terms of a specific job.
    A single global allowlist would have to be the union of every
    capability's needs, which is the same as having no allowlist.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: str | None = None
    allowed_base_urls: list[str] = Field(min_length=1)
    #: Glob patterns matched against the URL path. Empty means any path on
    #: an allowed origin -- a deliberate convenience for capabilities whose
    #: whole job lives on one site, not an invitation to leave it empty.
    allowed_routes: list[str] = Field(default_factory=list)
    allowed_action_types: list[ActionType] = Field(min_length=1)

    @field_validator("allowed_base_urls")
    @classmethod
    def _must_be_absolute(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if origin_of(url) is None:
                raise ValueError(
                    f"allowed_base_urls entry {url!r} is not an absolute URL "
                    "with a scheme and host"
                )
        return urls

    @property
    def allowed_origins(self) -> set[str]:
        """The base URLs reduced to comparable origins."""
        return {origin for url in self.allowed_base_urls if (origin := origin_of(url))}

    @classmethod
    def from_file(cls, path: Path | str) -> AllowlistConfig:
        """Load and validate a policy file.

        Validation happens here so a malformed policy fails when it is
        loaded -- before a browser is launched -- rather than at the moment
        it is first consulted, halfway through a run.
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)


class Decision(str, Enum):
    """The three ways a check can come out.

    ``needs_escalation`` is deliberately not a flavour of ``deny``. A denial
    says the action is out of bounds and the run should stop; an escalation
    says the action may well be correct but a human has to own it. Folding
    them together would either block legitimate work or quietly let
    irreversible actions through, depending on which way the fold went.
    """

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_ESCALATION = "needs_escalation"


class GuardrailViolation(RuntimeError):
    """Raised by raise_if_blocked() for callers that prefer to fail fast."""

    def __init__(self, result: "CheckResult") -> None:
        super().__init__(result.reason)
        self.result = result


class CheckResult(BaseModel):
    """What the guardrail concluded, and why.

    The reason is a required field rather than an optional nicety: a denial
    a human cannot explain is a denial that gets worked around.
    """

    decision: Decision
    reason: str
    action_type: ActionType
    risk_level: RiskLevel
    checked_url: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def needs_escalation(self) -> bool:
        return self.decision is Decision.NEEDS_ESCALATION

    def raise_if_blocked(self) -> CheckResult:
        """Convenience for call sites that want an exception, not a branch."""
        if not self.allowed:
            raise GuardrailViolation(self)
        return self
