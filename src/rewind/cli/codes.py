"""Process exit codes.

Kept in their own module because both the parser (which quotes them in ``--help``) and the
commands (which return them) need them, and neither should have to import the other.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
#: `diff --exit-code` and `revert --exit-code` use this to signal "live state is not what
#: the plan expects", which is what a CI job or a wrapper script wants to branch on. It is
#: deliberately distinct from EXIT_ERROR: nothing went wrong, the answer is just "no".
EXIT_CONFLICT = 3
