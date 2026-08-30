"""Escalation: pause, hand control to a human, resume.

Triggered by a stuck discovery loop, an unclassified replay failure, or an
irreversible step lacking approval (design notes §5).

Why the Playwright context is never closed on handoff: "escalation" that
tears down the session and asks a human to start over is a restart, not a
handoff. The load-bearing property is that the human drives the *same*
live page the automation was driving, then hands it back mid-flow. A
single `control_owner` field (automation | human), flipped atomically,
is the system's answer to "who is in control right now?".
"""
