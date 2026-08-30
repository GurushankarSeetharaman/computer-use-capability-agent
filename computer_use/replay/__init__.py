"""Replay engine: execute a capability artifact deterministically.

No LLM is involved. Steps run in order through the same guardrail as
discovery; after each one its checkpoint is asserted before advancing.

Why checkpoint mismatches are classified rather than raised: the three
outcomes are genuinely different kinds of thing (design notes §3).
A business outcome ("invalid credentials") is a legitimate answer the
caller asked for. A recoverable hiccup is noise to retry through. A hard
failure is a defect needing evidence and a human. Collapsing these into
one exception hierarchy with severity levels is the design mistake this
module exists to avoid -- so they are three distinct top-level statuses.
"""
