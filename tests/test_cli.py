"""Placeholder tests for computer_use.cli -- the command-line entry point.

Pins the not-yet-implemented contract: invoking the CLI today must fail
loudly. If someone later gives main() a real implementation, this test
fails and forces them to replace it with tests of actual behaviour, which
is exactly the prompt we want at that moment.
"""

import pytest

from computer_use.cli import main


def test_cli_is_not_yet_wired_up() -> None:
    with pytest.raises(NotImplementedError):
        main([])
