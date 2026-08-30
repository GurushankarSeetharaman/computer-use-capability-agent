# Computer-Use Automation System — Design Notes
For interface.ai take-home. Written before implementation so every design
decision is deliberate and defensible in interview follow-ups.

Target app: https://www.saucedemo.com (public QA sandbox, no ToS risk, no real PII)
Mechanism: Playwright, accessibility-tree-first locators, screenshots for LLM
observation + evidence
LLM: Claude (Anthropic API), tool-calling for the decide step
Language: Python

---

## 1. Architecture

Two execution modes share one core, split at the point where the LLM leaves
the loop:

```
┌─────────────── DISCOVERY (uses LLM) ───────────────┐
goal + target → Agent Loop → Recorder → Compiler → Capability Artifact
                (observe→decide→act,
                 Claude picks tool calls)

┌─────────────── REPLAY (no LLM) ───────────────┐
Capability Artifact + inputs → Replay Engine → Result
                                (deterministic,
                                 locator + fallbacks)
```

Shared components (used by both modes):

- **Surface adapter** — `perceive()` returns an accessibility-tree snapshot
  (pruned to interactive/labeled nodes) + screenshot; `act(action)` executes
  click/type/navigate/extract against Playwright. This is the seam that
  would swap out for a legacy-web or desktop adapter later (see §4).
- **Guardrail** — every single action, whether proposed by Claude or read
  from a replay step, passes through the allowlist + risk check before
  `act()` ever touches the page. Discovery and replay cannot bypass this.
- **Logger/Evidence** — structured JSONL log of every step (who decided it,
  what locator, what happened), screenshots on failure/escalation.
- **Escalation manager** — pause/handoff/resume, usable from either mode.

**Key trade-off:** single process, synchronous, CLI-driven, file-based
artifact storage (JSON on disk). No queues, no services. This is explicitly
what the brief asks for ("designing abstractions that *could* scale is
valuable; building the scaling infra is not") — be ready to say this in the
interview as a deliberate choice, not a shortcut you didn't think about.

**Why decouple Recorder from Compiler:** the agent loop produces a raw,
noisy transcript (retries, dead ends, the model's scratchpad reasoning).
The Compiler is a separate pass that distills *only the successful path*
into the clean artifact. This mirrors the brief's own language — artifact
must be "decoupled from the raw model transcript" — and gives you a clean
interview answer for "what if the discovery run had false starts?"

---

## 2. Artifact schema (the capability)

```jsonc
{
  "capability_id": "sauce_add_item_to_cart_checkout",
  "version": "1.0.0",
  "name": "Add item to cart and reach checkout review",
  "description": "Logs in, adds a named item to the cart, proceeds to the checkout review page.",
  "target": {
    "app_id": "saucedemo",
    "surface_type": "web",              // web | legacy_web | desktop
    "base_url": "https://www.saucedemo.com"
  },
  "inputs": [
    { "name": "item_name", "type": "string", "required": true },
    { "name": "username", "type": "string", "required": true },
    { "name": "password", "type": "string", "required": true, "sensitive": true }
  ],
  "outputs": [
    { "name": "cart_total", "type": "string", "source_step": "read_total" },
    { "name": "item_count", "type": "integer", "source_step": "read_count" }
  ],
  "steps": [
    {
      "step_id": "login",
      "action": "type_and_submit",
      "locator": {
        "strategy": "role+name",
        "primary": { "role": "textbox", "name": "Username" },
        "fallbacks": [{ "strategy": "css", "value": "#user-name" }]
      },
      "params": { "value": "${username}" },
      "checkpoint": { "type": "url_contains", "value": "/inventory.html" },
      "risk_level": "safe",
      "timeout_ms": 5000,
      "expected_business_outcomes": [
        { "match": { "type": "text", "value": "Epic sadface" }, "outcome_code": "invalid_credentials" }
      ]
    }
    // ...add_to_cart, open_cart, checkout_info, checkout_overview, read_total
  ],
  "success_checkpoint": { "type": "url_contains", "value": "/checkout-step-two.html" },
  "provenance": {
    "discovered_by": { "model": "claude-...", "run_id": "run_2026...", "timestamp": "..." },
    "approval_state": "draft"        // draft | approved  (stretch goal hook)
  },
  "redaction_policy": "default_v1"
}
```

**Why shaped this way:**
- `inputs`/`outputs` typed and named — this is the actual contract an AI
  agent calls, so it has to look like a function signature, not a step list.
- Each `locator` carries `primary` + `fallbacks` so replay degrades
  gracefully and *logs which one fired* — that log is your drift signal
  (see §3) and your multi-tenant reuse signal (see §4), for free.
- `expected_business_outcomes` lives on the step, not bolted on afterward —
  forces you to think about "what can legitimately go wrong here" at
  authoring time, which is the brief's central ask.
- `risk_level` per step, not per capability — a capability can mix safe
  reads with one irreversible write; guardrails need step granularity.
- `provenance.approval_state` costs nothing to add now and buys you the
  "confidence & approval" stretch goal later if you have time.

---

## 3. Determinism & error handling

**Locator strategy (in priority order):** accessibility role + accessible
name → `data-testid` if present → stable structural CSS → xpath. Try in
order, log which tier succeeded. A rising fallback rate for a capability
over time *is* your drift signal without any extra instrumentation.

**Waiting:** explicit wait-for-condition (element visible / network idle /
specific text present) with a bounded timeout. Never a bare `sleep`.

**Checkpoints:** after every step, assert the expected post-condition
before advancing. On mismatch, classify in this order:
1. Matches a step's `expected_business_outcomes` → **business outcome**
   (e.g. "invalid credentials", "item out of stock") — returned to the
   caller as a legitimate result, not an error.
2. Matches a known recoverable pattern (a dismissible interstitial, a
   transient spinner) → retry up to N times with backoff, log the recovery.
3. Anything else → **hard failure** — stop, capture screenshot + DOM
   snapshot, return `{step_id, expected, observed}`.

**Result contract:**
```jsonc
{ "status": "success", "outputs": {...} }
{ "status": "business_outcome", "code": "invalid_credentials", "detail": "..." }
{ "status": "failure", "step_id": "...", "expected": "...", "observed": "...", "evidence_path": "..." }
```
Keeping these three as distinct top-level states (not exceptions of varying
severity) is the single most important design choice in the whole system —
directly addresses the brief's "most common design mistake" callout.

---

## 4. Heterogeneity & multi-tenant (design only — not built)

**Surface abstraction:** `perceive()`/`act()` are the seam. A
`LegacyWebAdapter` would still expose an accessibility tree (browsers build
one even for frameset/table-based markup) with a heavier "locate by visible
text near a landmark" fallback strategy. A `DesktopAdapter` would swap
Playwright for an OS accessibility API (e.g. UI Automation on Windows,
`pywinauto`/AT-SPI) but expose the *same* role+name locator shape —
because the artifact schema encodes semantic locators (role, accessible
name), not surface-specific selectors, the same artifact structure survives
the swap. Only the adapter changes.

**Multi-tenant reuse:** capabilities are recorded against a semantic
locator vocabulary (role + accessible-name/label text), not tenant-specific
DOM paths — this is *why* §2's schema stores locators the way it does.
A capability recorded on one tenant's instance of a vendor product applies
to another tenant's instance of the same product via a thin **binding**
overlay: `{tenant_id, base_url, label_overrides}` — only branding/label
text differences need overriding, not the flow itself.

**Drift detection:** track fallback-tier usage rate per (capability,
tenant) pair from the replay logs already being written for §3. A
capability that starts silently falling back to tier 2/3 locators for one
tenant is a cheap, free-standing signal that tenant's app version has
drifted — no extra tooling required, it falls out of the logging you
already have.

---

## 5. Escalation & handoff

**Detection:** three triggers — (a) discovery agent reports low confidence
or repeats an action without progress for N steps, (b) replay hits an
unclassified failure (§3 case 3), (c) a step is flagged `risk_level:
irreversible` and lacks pre-approval.

**Intervention request:** `{run_id, capability_or_goal, current_step,
screenshot, reason, timestamp}` — enough for a human to act without
digging through logs.

**Control transfer (the load-bearing part):** automation pauses on the
*same* Playwright `BrowserContext`/page — it is never closed or replaced.
For this scope, expose that live page via Playwright's built-in Inspector /
CDP remote-debugging endpoint so a human can literally drive the same
session a browser window is already showing. Mock the "operator console"
as a bare local page with a Resume button; the real thing being demonstrated
is the control-transfer mechanism, not the console UI (explicitly
in-scope per the brief's scope note).

**Resume:** human clicks Resume → automation re-perceives current state →
discovery loop continues reasoning from there, or replay resumes at the
next step. Everything the human did while in control is captured in the
same evidence log, tagged `actor: human`.

**Who's in control:** a single `control_owner` field (`automation` |
`human`) on the run state, flipped atomically at handoff — this is the
answer to "how do you know who's in control."

---

## 6. Safety

- **Allowlist:** config per capability — permitted base URL(s)/routes and
  permitted action types. Checked by the guardrail before every `act()`
  call, regardless of whether the action came from Claude (discovery) or
  a replay step — one enforcement point, not two.
- **Risk handling:** `safe`/`reversible` actions proceed automatically.
  `irreversible` actions (anything that submits/commits/deletes) require
  either a pre-set approval flag on the artifact or trigger escalation —
  conservative by default, justified because this is regulated financial
  data in the real deployment target.
- **Redaction:** inputs marked `sensitive: true` in the schema (§2) are
  never written to the artifact or logs in plaintext — replaced with a
  redaction token before persistence; only held in memory for the duration
  of the call that needs them.
- **Limits:** this is allowlist + risk-tier + redaction, not a full policy
  engine — document that explicitly as a cut, with "policy-as-code /
  per-tenant policy override" as a named next step.

---

## 7. What to cut (be ready to name these deliberately)

- Legacy-web and desktop adapters — designed (§4), not built.
- Multi-tenant binding overlay — schema supports it (§2/§4), no second
  tenant implemented.
- Operator console — bare/mock UI; the control-transfer mechanism is real.
- Pick at most one stretch goal (§8 of the brief) if time remains —
  recommend **"Assisted fallback"** (bounded LLM recovery on a single
  replay failure) since it reinforces the core determinism story rather
  than adding a tangential feature.

---

## Interview prep — questions to be ready for

- Why role+name locators over CSS/xpath as primary? (stability across
  minor markup changes; same shape works on desktop accessibility trees)
- Why three result states instead of try/catch with error codes?
- Walk through one full request: business outcome vs. recoverable vs.
  hard failure — give a concrete example of each from your actual run.
- What breaks first if you had 1,000 tenants tomorrow? (per-tenant label
  drift outpacing the binding overlay's override list — you'd want the
  drift-detection signal from §4 feeding an alert, not just a log line)
- Where would you draw the line differently if this were a desktop app
  instead of web? (perceive/act adapter swap; UI Automation tree instead
  of Playwright's accessibility snapshot; same artifact schema)
- What's the one thing you'd build next with more time, and why that over
  the others?
