# Evidence

Three real runs against [saucedemo.com](https://www.saucedemo.com), plus the
capability artifact they concern. Everything here was produced by the code in
this repository; nothing is hand-written.

| Path | What it is |
| --- | --- |
| [`capability.json`](capability.json) | The compiled capability: 6 typed inputs, 1 output, 11 steps with locators, checkpoints and risk levels. This is what the two replays executed. |
| [`discovery/`](discovery) | A discovery run — the model driving the browser to the goal for the first time |
| [`replay-success/`](replay-success) | That capability replayed with valid inputs |
| [`replay-business-outcome/`](replay-business-outcome) | The same capability replayed with a bad username |

Each run directory holds `log.jsonl` (one JSON object per step) and
`screenshots/`. The discovery directory additionally holds `recording.json`,
the raw transcript, and `compiled-capability.json`, what that particular run
compiled to.

## What the three runs show

**`discovery/`** — goal met in 11 steps. The goal contains no credentials;
they were passed as structured parameters, and the model referred to them as
`${username}` / `${password}` without ever being shown the values.

**`replay-success/`** — every checkpoint passed, `order_total` extracted as
`Total: $32.39`, exit code 0. **No model was involved**: replay is an
interpreter walking the artifact.

**`replay-business-outcome/`** — one input changed to a bad username. The
`click_login` checkpoint genuinely failed, and the run returned:

```json
{
  "status": "business_outcome",
  "code": "invalid_credentials",
  "detail": "Epic sadface: Username and password do not match any user in this service"
}
```

Exit code 3, distinct from a failure's 1. The application said no, and that is
an answer the caller can act on — not a defect, and not something to retry.

## Two things worth looking for

**`click_add_to_cart` resolves on `fallback_1`, in both the discovery run and
the replay.** The inventory page has six identical "Add to cart" buttons, so
the primary role+name tier is genuinely ambiguous and the CSS fallback carries
it. Every step records which tier resolved it, so a capability that starts
falling back where it used to hit primary is announcing that the target
application has drifted.

**`recording.json` against `compiled-capability.json`.** The recording keeps
everything that happened; the artifact keeps only the path that worked. That
distillation is a separate compiler pass, which is why both are here.

## No credentials

Grep the whole directory for the password and you will not find it. The string
`password` does appear — as input names, step ids, the field's accessible
label, and the `${password}` placeholder — but never as a value.

One case is worth knowing about: the page reports the password field back in
its own accessibility tree, so a snapshot in `discovery/recording.json`
captured `role='textbox' name='Password' value='[REDACTED]'`. That value came
from the page rather than from anything the runner typed, and only a redactor
seeded before the run started could have caught it.
