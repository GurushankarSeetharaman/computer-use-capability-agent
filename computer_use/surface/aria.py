"""Parsing Playwright's ARIA snapshot text into structured nodes.

Playwright removed ``page.accessibility.snapshot()`` (which returned a
dict) in favour of ``page.aria_snapshot()``, which returns YAML-ish text::

    - text: Swag Labs
    - textbox "Username": standard_user
    - textbox "Password"
    - button "Login"
    - heading "Accepted usernames are:" [level=4]

Sticking with the browser's own accessibility computation -- rather than
walking the DOM ourselves and guessing at accessible names -- is what keeps
a perceived name and a ``get_by_role(name=...)`` locator in agreement. A
snapshot that disagreed with the locators would quietly invite the agent to
propose actions that cannot resolve.

The cost of that choice is this parser, and the benefit is that it is pure
text in, structures out: no browser needed to test it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A snapshot line: role, optional quoted accessible name, optional
#: bracketed attributes, optional trailing value. The quoted name is matched
#: before the trailing colon so that a name containing a colon -- such as
#: heading "Accepted usernames are:" -- does not split in the wrong place.
_LINE = re.compile(
    r"""^
    (?P<role>[A-Za-z][\w-]*)                  # role token
    (?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?       # optional "accessible name"
    (?P<attrs>(?:\s*\[[^\]]*\])*)             # optional [attr=value] groups
    (?:\s*:\s*(?P<value>.*))?                 # optional trailing value
    $""",
    re.VERBOSE,
)

#: YAML block-scalar markers. A line ending in one of these introduces
#: indented continuation lines rather than carrying a value of its own.
_BLOCK_MARKERS = frozenset({"|", ">", "|-", ">-", "|+", ">+"})


@dataclass(frozen=True)
class AriaEntry:
    """One node lifted out of the snapshot text."""

    role: str
    name: str | None
    value: str | None
    depth: int


def _unescape(text: str) -> str:
    """Undo the backslash escaping Playwright applies inside quoted names."""
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _unquote(value: str) -> str:
    """Strip the quotes YAML adds around a value that needs them.

    A value containing a colon comes back quoted -- the rejected-login
    banner arrives as ``text: "Epic sadface: Password is required"``.
    Leaving the quotes in place would put them inside the text an agent
    reasons about and a checkpoint matches against.
    """
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return _unescape(value[1:-1])
    return value


def parse_line(line: str) -> AriaEntry | None:
    """Parse a single snapshot line, or return None if it is not a node.

    Continuation lines from block scalars, blank lines, and anything that
    does not match the node shape are skipped rather than raising: a
    snapshot is an observation, and one unparseable line should cost that
    line, not the whole perception step.
    """
    stripped = line.strip()
    if not stripped.startswith("- "):
        return None

    depth = (len(line) - len(line.lstrip(" "))) // 2
    match = _LINE.match(stripped[2:].strip())
    if not match:
        return None

    value = match.group("value")
    if value is not None:
        value = value.strip()
        # An empty value means children follow; a marker means a block
        # scalar follows. Neither is a value this node actually holds.
        if not value or value in _BLOCK_MARKERS:
            value = None
        else:
            value = _unquote(value)

    name = match.group("name")
    return AriaEntry(
        role=match.group("role"),
        name=_unescape(name) if name is not None else None,
        value=value,
        depth=depth,
    )


def parse_aria_snapshot(snapshot: str) -> list[AriaEntry]:
    """Flatten an ARIA snapshot into an ordered list of entries.

    Document order is preserved and nesting is recorded as depth but not
    otherwise honoured: consumers of a snapshot want "what is on this page
    and what is it called", and reconstructing the tree would add structure
    nothing downstream reads.
    """
    entries = []
    for line in snapshot.splitlines():
        entry = parse_line(line)
        if entry is not None:
            entries.append(entry)
    return entries
