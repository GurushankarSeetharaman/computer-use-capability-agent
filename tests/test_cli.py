"""Tests for the command-line entry point.

Argument handling only -- driving a browser from here would just be the
replay tests again with more setup. What is worth pinning is the part
that is easy to get subtly wrong and security-relevant: where a value
comes from, and what happens when the place it should come from is empty.
"""

from __future__ import annotations

import pytest

from computer_use.cli import _env_pair, _pair, build_parser, main, resolve_values


# -- argument shapes -------------------------------------------------------


def test_pairs_split_on_the_first_equals_only() -> None:
    """A value may itself contain '=' -- a base64 token usually does."""
    assert _pair("username=standard_user") == ("username", "standard_user")
    assert _pair("token=abc=def==") == ("token", "abc=def==")


def test_a_pair_without_a_value_is_rejected() -> None:
    with pytest.raises(Exception):
        _pair("username")


def test_env_pairs_accept_an_explicit_variable_or_derive_one() -> None:
    assert _env_pair("password=TARGET_PASSWORD") == ("password", "TARGET_PASSWORD")
    assert _env_pair("password") == ("password", "PASSWORD")


# -- resolution ------------------------------------------------------------


def test_literals_and_environment_values_combine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TARGET_PASSWORD", "from-the-environment")
    values = resolve_values(
        [("username", "standard_user")], [("password", "TARGET_PASSWORD")]
    )
    assert values == {"username": "standard_user", "password": "from-the-environment"}


def test_an_environment_value_wins_over_a_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming both is a mistake; the safer source is the one that survives."""
    monkeypatch.setenv("TARGET_PASSWORD", "from-the-environment")
    values = resolve_values([("password", "on-the-command-line")],
                            [("password", "TARGET_PASSWORD")])
    assert values["password"] == "from-the-environment"


def test_a_missing_environment_variable_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an empty string.

    Supplying "" for a password produces a puzzling business outcome
    several steps later, rather than an obvious complaint here.
    """
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    with pytest.raises(SystemExit, match="DEFINITELY_NOT_SET"):
        resolve_values([], [("password", "DEFINITELY_NOT_SET")])


# -- the parser ------------------------------------------------------------


def test_both_commands_exist() -> None:
    parser = build_parser()
    for command in ("discover", "replay"):
        args = parser.parse_args(
            [command, "--goal", "g", "--target", "t"]
            if command == "discover"
            else [command, "--artifact", "a.json"]
        )
        assert args.command == command
        assert callable(args.handler)


def test_repeatable_inputs_accumulate() -> None:
    args = build_parser().parse_args(
        [
            "replay",
            "--artifact", "a.json",
            "--input", "username=standard_user",
            "--input", "item_name=Sauce Labs Backpack",
            "--input-env", "password=TARGET_PASSWORD",
        ]
    )
    assert args.input == [
        ("username", "standard_user"),
        ("item_name", "Sauce Labs Backpack"),
    ]
    assert args.input_env == [("password", "TARGET_PASSWORD")]


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_discover_requires_a_goal_and_a_target() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["discover", "--goal", "g"])


def test_discover_refuses_to_start_without_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing before a browser launches, with the fix in the message."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("computer_use.cli.load_environment", lambda *a, **k: None)

    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        main(["discover", "--goal", "g", "--target", "https://www.saucedemo.com"])
