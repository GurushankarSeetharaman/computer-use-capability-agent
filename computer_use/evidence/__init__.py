"""Evidence: structured run logs and screenshots.

Every run, discovery or replay, writes evidence/<run_id>/log.jsonl -- one
line per step: actor, action, locator tier used, checkpoint result,
timestamp -- plus screenshots on failure and escalation.

Why the locator tier is logged rather than merely used: a capability that
starts falling back from primary to tier-2 locators is telling you the
target app has drifted. Recording the tier turns routine logging into a
drift signal for free, with no extra instrumentation (design notes §4).
Redaction happens here too: anything marked `sensitive` in an artifact's
inputs is replaced before it is written, never after (design notes §6).
"""
