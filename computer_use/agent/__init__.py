"""Discovery loop: observe -> decide -> guardrail -> act -> record.

Claude picks one action per turn via tool calling, given the goal, the
current snapshot, and the history so far. The loop stops on `done`,
`report_stuck` (which escalates), max steps, or timeout.

The guardrail step in that chain is not a call this module makes -- it is
enforced inside surface.act(), so no decision the model reaches can bypass
policy. What the loop adds is a response to refusal: a blocked action is
recorded and fed back as a tool result, so the model adjusts instead of
retrying into the same wall.

Why the loop records everything, including its own mistakes: the raw
transcript is deliberately noisy -- retries, dead ends, abandoned
branches. Distilling it is the compiler's job, not this module's. Keeping
the two separate is what lets us answer "what if discovery had false
starts?" with "they are in the recording and absent from the artifact"
rather than with a cleanup heuristic buried in the loop (design notes
section 1).
"""

from computer_use.agent.loop import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
    DiscoveryAgent,
    DiscoveryResult,
)
from computer_use.agent.recording import (
    DiscoveryOutcome,
    RecordedStep,
    Recording,
    StepOutcome,
)
from computer_use.agent.tools import (
    CONTROL_TOOLS,
    DONE,
    REPORT_STUCK,
    TOOL_SCHEMAS,
    Decision,
    interpret,
)

__all__ = [
    "CONTROL_TOOLS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DONE",
    "Decision",
    "DiscoveryAgent",
    "DiscoveryOutcome",
    "DiscoveryResult",
    "REPORT_STUCK",
    "RecordedStep",
    "Recording",
    "SYSTEM_PROMPT",
    "StepOutcome",
    "TOOL_SCHEMAS",
    "interpret",
]
