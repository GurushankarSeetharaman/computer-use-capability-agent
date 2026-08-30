"""Computer-use automation: discover a capability once, replay it forever.

The system has two execution modes that share one core, split at the point
where the LLM leaves the loop (design notes §1):

  DISCOVERY  goal + target -> agent loop -> recorder -> compiler -> artifact
             (Claude decides each action)
  REPLAY     artifact + inputs -> replay engine -> result
             (deterministic; no model in the loop)

Why the split matters: the expensive, non-deterministic reasoning happens
once, at authoring time. Every subsequent execution is a plain interpreter
walking a JSON document, which is what makes runs auditable, cheap, and
repeatable. Anything that erodes that split -- e.g. letting replay call the
model to "figure it out" -- trades away the property the system exists for.
"""

__version__ = "0.1.0"
