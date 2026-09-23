"""Typed errors, so the CLI can report a cause without printing a traceback."""

from __future__ import annotations


class RewindError(Exception):
    """Base class for every error the tool raises deliberately."""


class PlanFileError(RewindError):
    """The plan document is missing, unreadable, or not a plan this build understands."""


class LiveStateError(RewindError):
    """A resource's current value could not be read.

    Deliberately not fatal: one unreadable resource must not stop the rest of a diff.
    """
