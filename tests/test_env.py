"""Tests for local environment loading.

The point of most of these is not that loading works -- python-dotenv is
not ours to test -- but that the two rules around it hold: a real exported
variable is never clobbered by a file, and no code path can be coaxed into
emitting a secret's value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from computer_use import env as env_module
from computer_use.env import (
    API_KEY_VAR,
    describe_api_key,
    has_api_key,
    load_environment,
    require_api_key,
)

#: Deliberately does not use a real key prefix. A realistic-looking dummy
#: trips secret scanners and push protection, and a test fixture is not
#: worth a false positive that trains people to click through warnings.
FAKE_KEY = "dummy-value-for-tests-0123456789"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the developer's own key leak into these assertions."""
    monkeypatch.delenv(API_KEY_VAR, raising=False)


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_values_from_a_dotenv_file(tmp_path: Path) -> None:
    path = write_env(tmp_path, f"{API_KEY_VAR}={FAKE_KEY}\n")
    assert load_environment(path) == path
    assert has_api_key()
    assert require_api_key() == FAKE_KEY


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Absence is normal: the key may already be exported."""
    assert load_environment(tmp_path / "nope.env") is None
    assert not has_api_key()


def test_an_exported_variable_outranks_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI and secrets managers must not lose to a stale local file."""
    monkeypatch.setenv(API_KEY_VAR, "exported-wins")
    load_environment(write_env(tmp_path, f"{API_KEY_VAR}=file-value\n"))
    assert require_api_key() == "exported-wins"


def test_override_is_available_when_explicitly_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_KEY_VAR, "exported")
    load_environment(write_env(tmp_path, f"{API_KEY_VAR}=file-value\n"), override=True)
    assert require_api_key() == "file-value"


# -- disclosure ------------------------------------------------------------


def test_describe_never_includes_the_value_or_any_part_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presence and length only -- not even a prefix."""
    monkeypatch.setenv(API_KEY_VAR, FAKE_KEY)
    described = describe_api_key()

    assert FAKE_KEY not in described
    for size in range(4, len(FAKE_KEY)):
        assert FAKE_KEY[:size] not in described
        assert FAKE_KEY[-size:] not in described
    assert "set" in described
    assert str(len(FAKE_KEY)) in described


def test_describe_reports_an_unset_key_plainly() -> None:
    assert describe_api_key() == f"{API_KEY_VAR}: not set"


def test_a_whitespace_only_key_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_VAR, "   \n")
    assert not has_api_key()


def test_require_api_key_explains_itself_without_quoting_a_value() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        require_api_key()
    message = str(excinfo.value)
    assert API_KEY_VAR in message
    assert ".env" in message


# -- import hygiene --------------------------------------------------------


def test_importing_the_package_does_not_load_the_environment() -> None:
    """Importing a library must not mutate the process environment.

    Otherwise merely importing the agent package would pick up whatever is
    in a local .env, and a real key could reach a run nobody meant to bill.
    """
    import subprocess
    import sys

    code = (
        "import os, computer_use.agent, computer_use.env; "
        f"print('LEAKED' if os.environ.get('{API_KEY_VAR}') else 'clean')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        env={"PATH": "", "SYSTEMROOT": ""},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "clean"


def test_repo_root_points_at_the_project(tmp_path: Path) -> None:
    assert (env_module.REPO_ROOT / "computer_use").is_dir()
    assert (env_module.REPO_ROOT / "requirements.txt").is_file()
