"""Command line interface.

    rewind undo  --identity X --since 90m [--confirm]      # all of the below, in order
    rewind scan  --identity X --since 90m
    rewind plan  --identity X --since 90m -o plan.json [--explain]
    rewind diff  plan.json [--blame]
    rewind revert plan.json [--confirm] [--only CHAIN_ID]
    rewind snapshot --instance i-... -o snapshot.json
    rewind operations
    rewind resolvers

Every command is read-only except ``revert --confirm`` and ``undo --confirm``, which reach the
same code. Credentials come from the ambient AWS configuration, the same as the AWS CLI, and
nothing is ever created in the account.

Three modules, split along the lines they are changed along: :mod:`.parser` declares the
interface, :mod:`.context` turns arguments into AWS objects, :mod:`.commands` joins them to
the pipeline. ``main`` below is only dispatch and the outermost error handler.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import List, Optional

from ..errors import RewindError
from ..timeutil import WindowError
from .codes import EXIT_CONFLICT, EXIT_ERROR, EXIT_OK, EXIT_USAGE
from .commands import COMMANDS
from .parser import build_parser

UTC = timezone.utc

__all__ = [
    "COMMANDS",
    "EXIT_CONFLICT",
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_USAGE",
    "build_parser",
    "main",
]


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    try:
        return COMMANDS[args.command](args, now or datetime.now(tz=UTC))
    except WindowError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except RewindError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a CLI should not show a traceback
        print("error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
