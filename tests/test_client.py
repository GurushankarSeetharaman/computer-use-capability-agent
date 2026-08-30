"""Tests for the Anthropic client factory.

No network: constructing a client makes no request, so the headers it
would send can be asserted directly.
"""

from __future__ import annotations

import pytest

from computer_use.agent.client import build_client, explain_api_error
from computer_use.env import WORKSPACE_HEADER, WORKSPACE_ID_VAR

WORKSPACE_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'anthropic-workspace-id is required when authenticating with an "
    "identity-linked API key; send the id of the workspace this request acts in.'}}"
)


def test_no_workspace_header_when_none_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(WORKSPACE_ID_VAR, raising=False)
    client = build_client(api_key="dummy")
    assert WORKSPACE_HEADER not in (client.default_headers or {})


def test_workspace_header_is_attached_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(WORKSPACE_ID_VAR, "wrkspc_example")
    client = build_client(api_key="dummy")
    assert client.default_headers[WORKSPACE_HEADER] == "wrkspc_example"


def test_explicit_headers_win_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(WORKSPACE_ID_VAR, "from-env")
    client = build_client(api_key="dummy", default_headers={WORKSPACE_HEADER: "explicit"})
    assert client.default_headers[WORKSPACE_HEADER] == "explicit"


def test_the_workspace_400_is_translated_into_an_instruction() -> None:
    """A 400 reads like a malformed request, not a configuration gap."""
    hint = explain_api_error(RuntimeError(WORKSPACE_400))
    assert hint is not None
    assert WORKSPACE_ID_VAR in hint
    assert ".env" in hint


def test_errors_that_already_explain_themselves_are_left_alone() -> None:
    assert explain_api_error(RuntimeError("Error code: 401 - invalid x-api-key")) is None
