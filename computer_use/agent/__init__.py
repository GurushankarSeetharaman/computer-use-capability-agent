"""Discovery loop: observe -> decide -> guardrail -> act -> record.

Claude picks one action per turn via tool calling, given the goal, the
current snapshot, and the history so far. The loop stops on `done`,
`report_stuck` (which escalates), max steps, or timeout.

Why the loop records everything, including its own mistakes: the raw
transcript is deliberately noisy -- retries, dead ends, abandoned
branches. Distilling it is the compiler's job, not this module's. Keeping
the two separate is what lets us answer "what if discovery had false
starts?" with "they are in the recording and absent from the artifact"
rather than with a cleanup heuristic buried in the loop (design notes §1).
"""
