"""Evidence: structured run logs and screenshots.

Every run, discovery or replay, writes evidence/<run_id>/log.jsonl -- one
line per step: actor, action, locator tier used, checkpoint result,
timestamp -- plus screenshots on failure and escalation.

Why the locator tier is logged rather than merely used: a capability that
starts falling back from primary to tier-2 locators is telling you the
target app has drifted. Recording the tier turns routine logging into a
drift signal for free, with no extra instrumentation (design notes
section 4).

Redaction happens on the way in, never as a later pass. A log that briefly
contained a password before being cleaned is a log that contained a
password (design notes section 6).
"""

from computer_use.evidence.log import EVIDENCE_ROOT, REDACTED, EvidenceLog

__all__ = ["EVIDENCE_ROOT", "REDACTED", "EvidenceLog"]
