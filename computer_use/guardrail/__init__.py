"""Guardrail: allowlist + risk check, enforced before every action.

Pure policy. No Playwright, no Anthropic SDK, no I/O beyond loading its
own config -- which is what makes it exhaustively unit-testable, and why
it is built before the components it protects.

Why one enforcement point instead of two: discovery actions come from a
model and replay actions come from a file, but both reach the page only
through `act()`, and both must pass this check first. Two checks would
eventually disagree; the interesting failure mode is the path that
forgets to call one. There is only one path (design notes §6).
"""
