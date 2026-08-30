# Build sequence — Claude Code prompts

Use these in order, in a fresh repo. Paste one prompt, let Claude Code
finish, **read the diff yourself before the next prompt** — each step
depends on you actually understanding the previous one, since you have to
defend all of it in interviews. Don't paste prompt 2 until you can explain
what prompt 1 produced.

General instruction to give Claude Code at the very start of the session
(paste once, before prompt 1):

> I'm working from a design doc (pasting relevant sections into each
> prompt as we go). Follow the architecture and schema exactly as
> specified rather than inventing your own — if you think a different
> approach is better, tell me why before implementing it, don't just do it.
> Keep functions small and each module independently testable. Add
> docstrings that explain *why*, not just what.

---

## Prompt 1 — Scaffolding

```
Set up a Python project for a computer-use automation system. Structure:

  computer_use/
    surface/         # perceive/act adapter, Playwright-backed
    agent/            # discovery loop, Claude tool-calling
    artifact/         # schema, compiler (recording -> artifact)
    replay/           # deterministic replay engine
    guardrail/        # allowlist + risk checks
    escalation/        # handoff manager
    evidence/         # structured logging, screenshots
    cli.py
  artifacts/          # saved capability JSON files, gitignored except examples
  evidence/           # run logs + screenshots, gitignored except examples
  tests/
  README.md
  REPORT.md           # empty, headings only for now — I'll write this myself
  requirements.txt    # playwright, anthropic, pydantic, pytest

Use pydantic for all schema/data models (artifact, result contract, etc.)
so validation is free. Set up pytest with a placeholder test in each
module. Don't implement any logic yet — just the skeleton, __init__.py
files, and a requirements.txt. Add a .gitignore for venv, __pycache__,
.env, evidence/*, artifacts/* (except an examples/ subfolder in each).
```

## Prompt 2 — Surface adapter

Paste the "Surface adapter" description from Architecture (§1) plus:

```
Implement computer_use/surface/ — a Playwright-backed adapter with two
methods:

- perceive() -> SurfaceSnapshot: returns a pruned accessibility-tree
  snapshot (only interactive/labeled nodes: role, accessible name,
  value, bounding box) as a pydantic model, plus a screenshot saved to
  disk and referenced by path.
- act(action: Action) -> ActionResult: executes one action against the
  live page. Support action types: navigate, click, type, select,
  extract (read text/value from an element), wait_for (condition:
  visible/text_present/url_contains, bounded timeout).

Locator resolution: an Action carries a locator with `strategy` +
`primary` + `fallbacks` (see schema below). act() tries primary first,
then each fallback in order, and returns which tier succeeded in
ActionResult so callers can log drift.

locator schema:
{ "strategy": "role+name" | "css" | "xpath" | "text",
  "primary": {...strategy-specific...},
  "fallbacks": [ {...}, ... ] }

Target for manual testing: https://www.saucedemo.com — write a quick
scratch script (not a test) that opens the site, perceives, and clicks
the login button by role+name, just so I can see it work end to end.
Don't build the agent loop yet.
```

## Prompt 3 — Guardrail

Paste the "Safety" section (§6) plus:

```
Implement computer_use/guardrail/ before the agent loop, since both
discovery and replay must call through it.

- AllowlistConfig: per-capability config of permitted base_url/route
  patterns and permitted action types. Load from a simple JSON/YAML file.
- RiskLevel: enum safe | reversible | irreversible, attached to an Action.
- Guardrail.check(action: Action, config: AllowlistConfig) -> CheckResult:
  raises/returns a clear denial if the action's target URL or action
  type isn't in the allowlist. For irreversible actions, require an
  explicit `approved: true` flag on the action or return a
  "needs_escalation" result rather than executing.

This should have zero dependency on Playwright or Claude — pure policy
logic, fully unit-testable. Write tests covering: allowed action passes,
disallowed route blocked, irreversible action without approval routes
to escalation, irreversible action with approval passes.
```

## Prompt 4 — Agent loop (discovery)

Paste "Architecture" §1's discovery diagram plus the artifact `inputs`
concept from §2, plus:

```
Implement computer_use/agent/ — the discovery loop.

Given a natural-language goal and a target (base_url), run:
  observe (surface.perceive()) -> decide (ask Claude, via tool calling,
  which single action to take next, given the goal + current snapshot +
  history of prior steps) -> guardrail.check() -> act (surface.act()) ->
  record the step -> repeat.

Stop conditions: goal met (Claude calls a `done` tool with a summary of
outputs), max_steps exceeded, timeout, or Claude reports it's stuck
(a `report_stuck` tool) — in which case hand off to
computer_use/escalation/ (stub this call for now, implement escalation
in a later prompt).

Define Claude's tools as: navigate, click, type, select, extract, wait_for,
done, report_stuck — matching the Action types from the surface adapter,
so the LLM's tool call maps directly to an Action object.

Use the Anthropic Python SDK, model claude-sonnet-4-6 (check
docs.claude.com if that string looks wrong — I want the current
recommended model, not a guess), tool_choice letting Claude pick.

Every (snapshot, decision, action, result) tuple gets appended to a raw
Recording (just a list of dicts is fine, this is the "raw transcript"
that the Compiler will later distill — don't try to clean it up here).

Test this against https://www.saucedemo.com with the goal "log in as
standard_user/secret_sauce and add the Sauce Labs Backpack to the cart,
then reach the checkout overview page."
```

## Prompt 5 — Artifact + Compiler

Paste the full "Artifact schema" section (§2) plus:

```
Implement computer_use/artifact/:

1. Pydantic models matching this schema exactly: [paste the JSON schema
   from §2 of the design doc]

2. A Compiler that takes a raw Recording (from prompt 4) and produces a
   Capability artifact:
   - Only include the successful path (drop retries/dead-ends from the
     recording — note in a docstring that this is deliberate, per the
     design doc's "decoupled from raw transcript" requirement).
   - Detect which concrete values used during discovery should become
     typed `inputs` (e.g. the item name, username, password) vs. which
     stay hardcoded as part of the flow. For this pass, prompt me
     interactively (simple CLI y/n) for each string literal used in an
     action: "treat 'Sauce Labs Backpack' as a parameter?" — don't try
     to fully automate parameter detection, that's a judgment call I
     want to make per-run.
   - Mark `password` (or anything matching a simple sensitive-field
     heuristic) with sensitive: true automatically.
   - Write the resulting artifact to artifacts/<capability_id>.json.

3. A save/load pair with pydantic validation on load, so a malformed
   artifact fails loudly rather than at replay time.
```

## Prompt 6 — Replay engine

Paste "Determinism & error handling" (§3) in full plus:

```
Implement computer_use/replay/ — deterministic replay, no LLM involved.

Given a Capability artifact path + a dict of input values:
- Validate inputs against the artifact's `inputs` schema (pydantic).
- Execute each step in order via surface.act(), through guardrail.check()
  exactly like discovery does.
- After each step, evaluate its `checkpoint`. On mismatch, classify in
  this order and stop as soon as one matches:
  1. matches an `expected_business_outcomes` pattern -> business_outcome
  2. matches a small built-in set of recoverable patterns (dismissible
     dialog, transient spinner) -> retry up to 3x with backoff, log it
  3. otherwise -> failure
- Return the Result contract exactly as specified in §3 (status: success
  | business_outcome | failure, with the fields listed there).
- On success, extract each declared `output` and include it in the result.
- Log locator tier used per step (primary vs fallback N) — this is the
  drift signal, make sure it's actually captured in the evidence log,
  not just printed.

Write a test that replays the artifact from prompt 5 twice: once with
valid inputs (expect success), once with a deliberately wrong username
(expect business_outcome, not failure — if saucedemo doesn't distinguish
"wrong username" from "wrong password" cleanly, use whatever business
outcome it does surface, e.g. the "Epic sadface" error banner).
```

## Prompt 7 — Escalation & handoff

Paste "Escalation & handoff" (§5) in full plus:

```
Implement computer_use/escalation/:

- InterventionRequest model: run_id, capability_or_goal, current_step,
  screenshot_path, reason, timestamp.
- EscalationManager.pause(context) — do NOT close the Playwright
  BrowserContext/page. Print/log the CDP remote-debugging URL for that
  context (or launch Playwright's Inspector) so I can manually attach
  and drive the live session.
- A minimal local "operator" CLI or bare HTML page with a single Resume
  button/command that flips a `control_owner` field on the run state
  from "automation" to "human" and back.
- On resume, re-perceive current state and continue the loop.
- Every action taken while control_owner == "human" gets logged to the
  same evidence trail, tagged actor: human.

Wire this into the `report_stuck` tool call from the discovery loop
(prompt 4) and into the "failure" path of the replay engine (prompt 6)
so both can trigger it. Give me a way to manually trigger escalation
too, for demo purposes (e.g. a CLI flag --force-escalate-at-step N).
```

## Prompt 8 — Evidence/logging polish

```
Review computer_use/evidence/ (should mostly exist already from prior
prompts — this pass is to make sure it's consistent). Every run
(discovery or replay) should produce:
  evidence/<run_id>/log.jsonl   — one line per step: actor, action,
                                   locator tier used, checkpoint result,
                                   timestamp
  evidence/<run_id>/screenshots/ — at minimum on failure/escalation,
                                    optionally at every step behind a
                                    --verbose flag

Add a redaction pass: any field marked sensitive in the artifact's
`inputs`, or matching a basic credential/PII regex, gets replaced with
[REDACTED] before it's written to log.jsonl or the artifact file. Write
a test that confirms a password never appears in any evidence file.
```

## Prompt 9 — CLI + README

```
Wire everything into computer_use/cli.py with two commands:

  python -m computer_use discover --goal "..." --target "https://www.saucedemo.com" --out artifacts/
  python -m computer_use replay --artifact artifacts/<id>.json --input item_name="Sauce Labs Backpack" --input username=standard_user --input password=secret_sauce

Then write README.md covering:
- setup (venv, requirements, playwright install, ANTHROPIC_API_KEY)
- the two commands above as the demo path, verbatim, so anyone can copy-paste
- how to run without live services (point at the recorded example
  artifact + evidence in examples/ for a dry read-through)
- how to run tests

Don't touch REPORT.md — I'm writing that myself.
```

## Prompt 10 — Generate the required evidence

```
Run two real end-to-end passes and save the results under evidence/, then
copy the artifact + both evidence folders into a top-level /evidence/
directory as the assignment requires:

1. A discovery run for the login+add-to-cart+checkout goal (from prompt 4).
2. A replay of the resulting artifact with valid inputs (success).
3. A second replay with a deliberately bad input (business_outcome) —
   this is the "one replay that hits an error/exceptional state" the
   assignment explicitly asks for.

Show me the three resulting log.jsonl files and confirm no secrets
appear in them (spot check for "secret_sauce" or "password" strings).
```

---

## After this: your job, not Claude Code's

- **REPORT.md** — write this yourself, using DESIGN_NOTES.md §1–7 as your
  outline, but in your own words and updated with whatever you actually
  changed while building (things never survive contact with implementation
  unchanged — note where reality diverged from the plan and why).
- **Read every file Claude Code wrote once, end to end**, before you
  consider it done. If something in it surprises you, ask Claude Code to
  explain that specific piece back to you until it doesn't.
- **The presentation** — once the code's done, tell me and I'll help you
  structure a walkthrough deck (architecture → schema → a live-feeling
  demo trace → error handling → escalation → what you'd build next).
  That'll double as your interview rehearsal.
