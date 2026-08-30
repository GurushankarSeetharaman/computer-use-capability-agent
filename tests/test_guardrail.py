"""Placeholder tests for computer_use.guardrail -- the allowlist and risk checks.

Asserts the module imports cleanly and carries the design rationale its
docstring is supposed to hold. Thin, but not worthless: it catches syntax
errors and an undocumented module from the very first commit, and gives
the real tests for this component somewhere to land.
"""

import computer_use.guardrail as subject


def test_module_imports() -> None:
    assert subject is not None


def test_module_documents_its_purpose() -> None:
    assert subject.__doc__, "every module states why it exists, not just what it does"
