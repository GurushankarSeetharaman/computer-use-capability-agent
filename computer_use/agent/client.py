"""Constructing the Anthropic client with this project's configuration.

Kept out of loop.py deliberately: the discovery loop takes an injected
client and never imports the SDK, which is what lets its whole control
flow be tested with no API key and no network. This module is the one
place that knows the SDK exists, so entry points get a correctly
configured client without the loop growing a dependency on one.
"""

from __future__ import annotations

from typing import Any

import anthropic

from computer_use.env import WORKSPACE_ID_VAR, client_headers

#: Substring of the API's complaint when an identity-linked key is used
#: without naming a workspace. Matched to turn a puzzling 400 into an
#: instruction, since the message reads like a malformed request rather
#: than the configuration gap it is.
_WORKSPACE_ERROR_MARKER = "anthropic-workspace-id"


def build_client(**kwargs: Any) -> anthropic.Anthropic:
    """An Anthropic client carrying whatever headers this setup requires."""
    headers = client_headers()
    if headers:
        kwargs["default_headers"] = {**headers, **(kwargs.get("default_headers") or {})}
    return anthropic.Anthropic(**kwargs)


def explain_api_error(error: Exception) -> str | None:
    """Turn a known-confusing API error into an actionable instruction.

    Returns None for errors that already explain themselves; there is no
    value in paraphrasing a message that was already clear.
    """
    if _WORKSPACE_ERROR_MARKER in str(error):
        return (
            "This API key is identity-linked, so every request must name the "
            f"workspace it acts in. Add {WORKSPACE_ID_VAR}=<your workspace id> to "
            "the .env file at the repo root. You can find the id in the Anthropic "
            "Console under Settings -> Workspaces (it looks like wrkspc_...). "
            "Alternatively, create a standard (non identity-linked) API key, which "
            "needs no workspace id."
        )
    return None
