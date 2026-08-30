# Computer-Use Capability Agent

An LLM drives a browser to accomplish a goal, the successful run is compiled
into a reusable **capability artifact**, and that artifact is then replayed
deterministically with no model in the loop.

Target app for the demo: [saucedemo.com](https://www.saucedemo.com) (public QA
sandbox — no real data, no ToS concerns).

## Status

Scaffolding only. Module skeletons and their design rationale are in place;
no behaviour is implemented yet. Components land one at a time, in the order
set out in [docs/CLAUDE_CODE_PROMPTS.md](docs/CLAUDE_CODE_PROMPTS.md).

## Layout

| Path | Role |
| --- | --- |
| `computer_use/surface/` | Playwright-backed `perceive()` / `act()` adapter — the only module that knows what a browser is |
| `computer_use/agent/` | Discovery loop: observe → decide (Claude) → act → record |
| `computer_use/artifact/` | Capability schema, and the compiler that distils a recording into one |
| `computer_use/replay/` | Deterministic replay engine — no LLM |
| `computer_use/guardrail/` | Allowlist + risk checks; pure policy, no I/O |
| `computer_use/escalation/` | Pause, hand the live session to a human, resume |
| `computer_use/evidence/` | Structured JSONL logs, screenshots, redaction |
| `config/` | Per-capability allowlist policy files (JSON) |
| `artifacts/` | Generated capability JSON (gitignored except `examples/`) |
| `evidence/` | Generated run logs and screenshots (gitignored except `examples/`) |

## Design documents

- [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) — architecture, artifact schema,
  error taxonomy, escalation model, and safety design. Written before
  implementation; the source of truth for what this system is meant to be.
- [REPORT.md](REPORT.md) — the write-up, authored by hand.

## Running the tests

```bash
python -m pytest
```

Setup instructions, the copy-pasteable demo commands, and the offline
read-through path are added once the CLI exists.
