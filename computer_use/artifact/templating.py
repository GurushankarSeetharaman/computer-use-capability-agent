"""Substituting input values into a capability's steps.

A compiled capability is a template: the values a discovery run happened
to use become named inputs, and the steps refer to them as ``${name}``.
Replay renders those placeholders against the inputs it was called with.

Placeholders appear in two places, and the second one needs justifying.
In ``params`` they are ordinary -- the text typed into a field is obviously
a parameter. In a **locator** they are not obvious, and are used here for
one specific case: saucedemo builds its add-to-cart buttons with ids of
the form ``#add-to-cart-sauce-labs-backpack``. The product being added is
encoded in the element id rather than in any value the run typed, so a
capability that could add *any* item has to substitute into the locator.

The ``|slug`` filter exists for exactly that, which is why it is the only
filter: ``${item_name|slug}`` turns "Sauce Labs Backpack" into
"sauce-labs-backpack".

**This assumes saucedemo's id-naming convention and does not generalize.**
Another application might use numeric ids, a different separator, or no
id at all, and this would silently produce a locator that matches nothing.
It is deliberately kept to one substitution function so the assumption
lives in one readable place; a real multi-tenant system would express it
as part of the binding overlay in design notes section 4 rather than as a
string rule compiled into an artifact.
"""

from __future__ import annotations

import re

#: ``${name}`` or ``${name|slug}``. Input names are snake_case by
#: convention, which keeps the pattern tight enough that a stray "${" in
#: page text cannot be mistaken for a placeholder.
PLACEHOLDER = re.compile(r"\$\{([a-z_][a-z0-9_]*)(?:\|(slug))?\}")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, hyphen-separated form of a human label."""
    return _NON_ALNUM.sub("-", value.lower()).strip("-")


def placeholders(text: str) -> set[str]:
    """Every input name referenced by a template string."""
    return {match.group(1) for match in PLACEHOLDER.finditer(text)}


def render(text: str, values: dict[str, object]) -> str:
    """Substitute input values into a template string.

    Raises KeyError naming the missing input rather than leaving the
    placeholder in place: a locator that still contains "${item_name}" at
    execution time would fail as "element not found", sending whoever
    debugs it to the page instead of to the missing argument.
    """

    def substitute(match: re.Match[str]) -> str:
        name, filter_name = match.group(1), match.group(2)
        if name not in values:
            raise KeyError(name)
        rendered = str(values[name])
        return slugify(rendered) if filter_name == "slug" else rendered

    return PLACEHOLDER.sub(substitute, text)
