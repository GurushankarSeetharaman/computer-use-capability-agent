"""Placeholder tests for computer_use.surface -- the Playwright-backed perceive/act adapter.

Asserts the module imports cleanly and carries the design rationale its
docstring is supposed to hold. Thin, but not worthless: it catches syntax
errors and an undocumented module from the very first commit, and gives
the real tests for this component somewhere to land.
"""

import computer_use.surface as subject


def test_module_imports() -> None:
    assert subject is not None


def test_module_documents_its_purpose() -> None:
    assert subject.__doc__, "every module states why it exists, not just what it does"
