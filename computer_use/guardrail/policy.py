"""The check itself: one function, called before every act().

Pure policy. No Playwright, no Anthropic SDK, no I/O beyond the config
that was already loaded. That is what lets every branch below be tested
exhaustively in milliseconds, which matters more here than anywhere else
in the system -- this is the code that is supposed to stop the expensive
mistakes.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from computer_use.guardrail.models import (
    AllowlistConfig,
    CheckResult,
    Decision,
    origin_of,
    path_of,
)
from computer_use.surface.models import Action, ActionType, RiskLevel


class Guardrail:
    """Allowlist and risk enforcement for a single action.

    Stateless by design. Discovery actions come from a model and replay
    actions come from a file, but both reach the page through the same
    act() call and so through the same check here. A guardrail that held
    state would invite one caller's history to affect another caller's
    verdict (design notes section 6).
    """

    @staticmethod
    def check(
        action: Action,
        config: AllowlistConfig,
        *,
        current_url: str | None = None,
    ) -> CheckResult:
        """Decide whether one action may proceed.

        ``current_url`` is where the page is *now*. It is required for
        everything except navigate, because a click has no target URL of
        its own -- it acts on wherever the browser already is, and an
        allowlist that only inspected navigations would be trivially
        sidestepped by navigating somewhere permitted and then clicking a
        link to somewhere that is not.

        Checks run in order of how conclusive they are: an action that is
        the wrong *kind* of thing is rejected before we ask where it points,
        and an out-of-bounds target is rejected before we ask who may
        approve it -- there is no approval for a step that should not be
        happening at all.
        """
        verdict = _check_action_type(action, config)
        if verdict is not None:
            return verdict

        target = action.url if action.type is ActionType.NAVIGATE else current_url
        verdict = _check_target(action, config, target)
        if verdict is not None:
            return verdict

        return _check_risk(action, target)


def _result(action: Action, decision: Decision, reason: str, url: str | None) -> CheckResult:
    return CheckResult(
        decision=decision,
        reason=reason,
        action_type=action.type,
        risk_level=action.risk_level,
        checked_url=url,
    )


def _check_action_type(action: Action, config: AllowlistConfig) -> CheckResult | None:
    """Reject verbs this capability was never meant to use."""
    if action.type in config.allowed_action_types:
        return None
    permitted = ", ".join(sorted(t.value for t in config.allowed_action_types))
    return _result(
        action,
        Decision.DENY,
        f"action type {action.type.value!r} is not permitted for this capability "
        f"(allowed: {permitted})",
        None,
    )


def _check_target(
    action: Action, config: AllowlistConfig, target: str | None
) -> CheckResult | None:
    """Reject actions pointing outside the allowed origins and routes."""
    if target is None:
        # Not knowing where an action lands is itself disqualifying: we
        # cannot show it is in bounds, and "probably fine" is not a policy.
        return _result(
            action,
            Decision.DENY,
            f"cannot verify the target of a {action.type.value!r} action: "
            "no current_url was supplied",
            None,
        )

    origin = origin_of(target)
    if origin is None:
        return _result(
            action, Decision.DENY, f"target {target!r} is not an absolute URL", target
        )
    if origin not in config.allowed_origins:
        permitted = ", ".join(sorted(config.allowed_origins))
        return _result(
            action,
            Decision.DENY,
            f"origin {origin!r} is not allowlisted (allowed: {permitted})",
            target,
        )

    if config.allowed_routes and not _route_permitted(target, config.allowed_routes):
        permitted = ", ".join(config.allowed_routes)
        return _result(
            action,
            Decision.DENY,
            f"route {path_of(target)!r} does not match any allowed route "
            f"({permitted})",
            target,
        )
    return None


def _route_permitted(url: str, patterns: list[str]) -> bool:
    """Glob-match a URL path against the allowed routes.

    fnmatchcase rather than fnmatch: plain fnmatch takes its case
    sensitivity from the host OS, which would make this policy
    case-insensitive on Windows and case-sensitive in CI. A guardrail whose
    verdict depends on where it runs is not a guardrail.
    """
    path = path_of(url)
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _check_risk(action: Action, target: str | None) -> CheckResult:
    """Gate irreversible actions behind explicit approval.

    Safe and reversible actions proceed. An irreversible one proceeds only
    if it was approved in advance; otherwise it escalates rather than
    failing, because the action is probably correct and what it lacks is a
    human willing to own it (design notes section 6).
    """
    if action.risk_level is not RiskLevel.IRREVERSIBLE:
        return _result(
            action,
            Decision.ALLOW,
            f"{action.risk_level.value} action within the allowlist",
            target,
        )
    if action.approved:
        return _result(
            action, Decision.ALLOW, "irreversible action carries prior approval", target
        )
    return _result(
        action,
        Decision.NEEDS_ESCALATION,
        "irreversible action has no prior approval; a human must confirm it",
        target,
    )
