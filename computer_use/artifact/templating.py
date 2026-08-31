"""Re-export of the shared templating helpers.

The implementation moved to computer_use.templating so that the agent can
use it too: computer_use.artifact imports computer_use.agent.recording, so
an import in the other direction would close a cycle. This module stays so
that `from computer_use.artifact.templating import render` keeps working.
"""

from computer_use.templating import (
    PLACEHOLDER,
    placeholders,
    render,
    render_partial,
    slugify,
)

__all__ = ["PLACEHOLDER", "placeholders", "render", "render_partial", "slugify"]
