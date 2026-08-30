"""Capability artifact: the schema, and the compiler that produces one.

The artifact is the deliverable of a discovery run -- a typed, versioned
JSON document describing how to accomplish one goal, with named `inputs`
and `outputs` so a caller reads it as a function signature rather than as
a step list (design notes §2).

Why the compiler is a separate pass rather than a mode of the agent loop:
the artifact must be decoupled from the raw model transcript. A separate
pass takes the recording as input and emits only the successful path, so
the distillation rules live in one readable place and can be reasoned
about (and tested) without running a browser or a model.
"""
