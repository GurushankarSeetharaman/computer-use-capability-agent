# Computer-Use Capability Agent

An LLM drives a browser to accomplish a goal once. The successful path is
compiled into a reusable **capability artifact**, and that artifact is then
replayed deterministically — with no model in the loop.

```
DISCOVERY (uses the model)     goal + target → agent loop → recorder → compiler → artifact
REPLAY    (no model)           artifact + inputs → replay engine → result
```

The split is the point. The expensive, non-deterministic reasoning happens
once, at authoring time. Every subsequent execution is an interpreter walking
a JSON document, which is what makes runs auditable, repeatable and cheap.

Target app for the demo: [saucedemo.com](https://www.saucedemo.com) — a public
QA sandbox, no real data.

Design rationale lives in [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md); the
write-up is [REPORT.md](REPORT.md).

---

## Setup

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

```bash
.venv/Scripts/python.exe -m playwright install chromium
```

On macOS or Linux, use `.venv/bin/python` throughout instead.

**An API key is needed for `discover` only.** Replay never calls a model. Put
it in a `.env` file at the repository root:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

If your key is identity-linked, add `ANTHROPIC_WORKSPACE_ID=wrkspc_...` as
well — the API rejects such keys with a 400 that reads like a malformed
request rather than a configuration gap, so the CLI translates it for you.

---

## The demo path

### 1. Discover a capability

```bash
.venv/Scripts/python.exe -m computer_use discover --goal "log in, add the Sauce Labs Backpack to the cart, then reach the checkout overview page" --target "https://www.saucedemo.com" --out artifacts/ --credential username=standard_user --credential password=secret_sauce
```

Add `--headed` to watch the browser. The model chooses each action, the run is
recorded, and the compiler then asks which literals should become typed inputs
(`--non-interactive` accepts its proposals instead).

Note the goal contains **no credentials**. They are passed separately, and the
model never sees their values — it refers to them as `${username}` and
`${password}`, and the runner substitutes at the moment of typing. A secret
that never enters the conversation cannot be echoed back in a summary or
retained in a transcript.

### 2. Replay it

```bash
.venv/Scripts/python.exe -m computer_use replay --artifact artifacts/examples/saucedemo_add_item_to_cart_checkout.json --input item_name="Sauce Labs Backpack" --input username=standard_user --input password=secret_sauce --input first_name=John --input last_name=Doe --input zip_postal_code=12345
```

```json
{
  "status": "success",
  "outputs": { "order_total": "Total: $32.39" }
}
```

No API key, no model, ~12 seconds.

### 3. See an error state handled

Change one input and the run comes back with a **business outcome**, not a
failure:

```bash
.venv/Scripts/python.exe -m computer_use replay --artifact artifacts/examples/saucedemo_add_item_to_cart_checkout.json --input item_name="Sauce Labs Backpack" --input username=wrong_user --input password=secret_sauce --input first_name=John --input last_name=Doe --input zip_postal_code=12345
```

```json
{
  "status": "business_outcome",
  "code": "invalid_credentials",
  "detail": "Epic sadface: Username and password do not match any user in this service"
}
```

The application said no, and that is an answer — not a defect, not something
to retry. Exit codes follow: `0` success, `3` business outcome, `1` failure.

---

## Passing real secrets

`secret_sauce` is saucedemo's published test credential, printed on its own
login page, so putting it on the command line above is harmless. **A real
credential should not go there** — command lines land in shell history and are
readable from the process list by anyone on the machine.

Both commands accept an environment variable instead:

```bash
.venv/Scripts/python.exe -m computer_use replay --artifact artifacts/examples/saucedemo_add_item_to_cart_checkout.json --input username=standard_user --input-env password=TARGET_PASSWORD --input item_name="Sauce Labs Backpack" --input first_name=John --input last_name=Doe --input zip_postal_code=12345
```

`--input-env password=TARGET_PASSWORD` reads input `password` from `$TARGET_PASSWORD`.
Bare `--input-env password` reads `$PASSWORD`. `discover` takes the same form as
`--credential-env`. A named variable that is not set is an error, not an empty
string — supplying `""` for a password produces a puzzling business outcome
several steps later instead of an obvious complaint up front.

**This is the recommended form for anything that is actually a secret.**

---

## Reading it without running anything

No API key, no browser, no network required — the repository ships a worked
example of every stage:

| Path | What it is |
| --- | --- |
| [`artifacts/examples/saucedemo_add_item_to_cart_checkout.json`](artifacts/examples/saucedemo_add_item_to_cart_checkout.json) | A compiled capability: typed inputs, outputs, 11 steps with locators, checkpoints and risk levels |
| [`evidence/discovery/`](evidence/discovery) | A real discovery run — `log.jsonl`, the raw `recording.json`, a screenshot |
| [`evidence/replay-success/`](evidence/replay-success) | A replay that succeeded |
| [`evidence/replay-business-outcome/`](evidence/replay-business-outcome) | A replay that hit `invalid_credentials` |
| [`evidence/README.md`](evidence/README.md) | What each run shows, and what to look for in it |

Things worth looking for while reading:

- **`recording.json` vs. the artifact.** The recording keeps everything that
  happened, including dead ends the model backed out of. The artifact contains
  only the successful path. That distillation is the compiler's job, and
  keeping both is what lets "what if discovery had false starts?" be answered
  with evidence.
- **`locator_tier` in `log.jsonl`.** Every step records which locator tier
  resolved it. `click_add_to_cart` comes back as `fallback_1` because the
  inventory page has six identical "Add to cart" buttons, so the primary
  role+name tier is genuinely ambiguous. A rising fallback rate over time is a
  drift signal, for free.
- **No credentials anywhere.** Grep the evidence directory for a password and
  you will not find one.

## Running the tests

```bash
.venv/Scripts/python.exe -m pytest
```

229 tests, no network and no browser — the surface adapter, locator
resolution, guardrail, agent loop, compiler and replay engine are all
exercised against fakes, so the states that matter (an ambiguous locator, a
refused action, a spinner still up on the second attempt) can be tested at
all.

Two tests drive a real browser and are excluded by default:

```bash
.venv/Scripts/python.exe -m pytest -m live
```

## Layout

| Path | Role |
| --- | --- |
| `computer_use/surface/` | Playwright-backed `perceive()` / `act()` — the only module that knows what a browser is |
| `computer_use/agent/` | Discovery loop: observe → decide (Claude) → act → record |
| `computer_use/artifact/` | Capability schema, and the compiler that distils a recording into one |
| `computer_use/replay/` | Deterministic replay engine — no LLM |
| `computer_use/guardrail/` | Allowlist + risk checks; pure policy, no I/O |
| `computer_use/escalation/` | Pause, hand the live session to a human, resume |
| `computer_use/evidence/` | Structured JSONL logs, screenshots, redaction |
| `config/` | Per-capability allowlist policy |
| `scripts/` | Manual end-to-end checks, and the script that regenerates the committed example |

Generated run output is gitignored. `artifacts/examples/` and the `evidence/`
bundle are committed on purpose, so the repository can be read without
running anything.
