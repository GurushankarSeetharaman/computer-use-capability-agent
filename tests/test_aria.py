"""Unit tests for the ARIA snapshot parser.

Playwright returns the accessibility tree as YAML-ish text, so perceiving a
page means parsing it. Every case below is real output shape captured from
https://www.saucedemo.com -- which is the point of testing it here: the
parser is pure text in, structures out, so the fiddly cases (a name
containing a colon, a value quoted because it contains one) get covered in
milliseconds without launching a browser.
"""

from __future__ import annotations

from computer_use.surface.aria import parse_aria_snapshot, parse_line

LOGIN_PAGE = """
- text: Swag Labs
- textbox "Username": standard_user
- textbox "Password"
- button "Login"
- heading "Accepted usernames are:" [level=4]
- text: secret_sauce
"""


def test_role_only_line() -> None:
    entry = parse_line('- button "Login"')
    assert entry is not None
    assert (entry.role, entry.name, entry.value) == ("button", "Login", None)


def test_named_node_with_a_value() -> None:
    entry = parse_line('- textbox "Username": standard_user')
    assert entry is not None
    assert (entry.role, entry.name, entry.value) == ("textbox", "Username", "standard_user")


def test_unnamed_text_node_carries_its_content_as_value() -> None:
    entry = parse_line("- text: Swag Labs")
    assert entry is not None
    assert (entry.role, entry.name, entry.value) == ("text", None, "Swag Labs")


def test_name_containing_a_colon_is_not_split_at_the_colon() -> None:
    """The quoted name is matched before the trailing-value colon."""
    entry = parse_line('- heading "Accepted usernames are:" [level=4]')
    assert entry is not None
    assert entry.role == "heading"
    assert entry.name == "Accepted usernames are:"
    assert entry.value is None


def test_quoted_value_is_unquoted() -> None:
    """The rejected-login banner: YAML quotes it because it has a colon."""
    entry = parse_line('- text: "Epic sadface: Password is required"')
    assert entry is not None
    assert entry.value == "Epic sadface: Password is required"


def test_container_line_has_no_value() -> None:
    entry = parse_line("- banner:")
    assert entry is not None
    assert entry.role == "banner"
    assert entry.value is None


def test_block_scalar_marker_is_not_a_value() -> None:
    entry = parse_line("- text: |")
    assert entry is not None
    assert entry.value is None


def test_escaped_quotes_in_a_name_are_unescaped() -> None:
    entry = parse_line('- button "Say \\"hi\\""')
    assert entry is not None
    assert entry.name == 'Say "hi"'


def test_indentation_is_recorded_as_depth() -> None:
    assert parse_line('- button "a"').depth == 0
    assert parse_line('  - button "a"').depth == 1
    assert parse_line('    - button "a"').depth == 2


def test_non_node_lines_are_skipped_rather_than_raising() -> None:
    """One unparseable line should cost that line, not the whole snapshot."""
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("not a node") is None
    assert parse_line("- ") is None


def test_full_snapshot_parses_in_document_order() -> None:
    entries = parse_aria_snapshot(LOGIN_PAGE)
    assert [entry.role for entry in entries] == [
        "text",
        "textbox",
        "textbox",
        "button",
        "heading",
        "text",
    ]
    assert entries[1].name == "Username"
    assert entries[1].value == "standard_user"
    assert entries[3].name == "Login"
