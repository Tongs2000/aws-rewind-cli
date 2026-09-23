"""Building an inverse call for a field no plugin knows.

For a **declarative, partial-update** API the inverse is mechanical: call the same
operation, on the same resource, with the same field set to its old value.

    ModifyDBInstance(dBInstanceIdentifier=db, multiAZ=true)
      → ModifyDBInstance(DBInstanceIdentifier=db, MultiAZ=false)

That covers a large share of ``Modify*`` / ``Update*`` / ``Set*`` APIs. Four families break
it, and each is refused rather than guessed at:

1. **whole-document replacement.** ``PutBucketPolicy`` and friends replace the entire
   object, so calling one with a single field would delete everything else. Any inverse
   built from a partial view of such an API is destructive.
2. **state encoded in the verb.** ``MonitorInstances`` / ``UnmonitorInstances`` -
   the old value is the *other* event name, which requires knowing the pair.
3. **preconditions.** An EC2 resize needs the instance stopped first. A bare
   ``ModifyInstanceAttribute`` would simply fail, or worse, half-apply.
4. **creation and deletion.** The inverse of ``DeleteX`` is ``CreateX`` with every
   argument the original had, which CloudTrail does not record.

So the generic tier emits a command for a human to read and run, and never executes it.
That is the whole reason :class:`~rewind.models.Capability` distinguishes MANUAL from AUTO.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

from ...domain import LIST_SEPARATOR

#: Event-name prefixes whose call replaces a whole document rather than merging fields.
WHOLE_DOCUMENT_PREFIXES = ("Put", "Replace", "Import", "Restore", "Overwrite")

#: Event-name prefixes that create or destroy, where no symmetric inverse exists.
LIFECYCLE_PREFIXES = (
    "Create", "Delete", "Terminate", "Run", "Register", "Deregister", "Allocate",
    "Release", "Attach", "Detach", "Associate", "Disassociate", "Add", "Remove",
    "Copy", "Cancel", "Start", "Stop", "Reboot", "Revoke", "Authorize",
)

#: Prefixes that merge the fields you pass, leaving the rest alone.
PARTIAL_UPDATE_PREFIXES = ("Modify", "Update", "Set", "Change", "Configure", "Enable", "Disable")

#: Two-pass camel splitting. A single boundary regex mangles runs of capitals:
#: ``ModifyDBInstance`` must become ``modify-db-instance``, not ``modify-d-b-instance``.
_BEFORE_WORD = re.compile(r"(.)([A-Z][a-z]+)")
_AFTER_LOWER = re.compile(r"([a-z0-9])([A-Z])")

#: Structural wrappers that are not part of a parameter's name. EC2 nests every attribute
#: as ``{"disableApiTermination": {"value": true}}``, so the last path segment is the
#: wrapper, not the flag - emitting ``--value`` would produce a command that fails.
WRAPPER_SEGMENTS = ("value", "values", "item", "items")

#: Values the AWS CLI expresses as a flag pair (``--x`` / ``--no-x``) rather than an argument.
BOOLEAN_VALUES = {"true": True, "false": False}
#: CloudTrail appends an API date to some event names - UpdateFunctionConfiguration20150331v2
#: - which is not part of the CLI command. Stripped so the emitted command is runnable.
_API_VERSION_SUFFIX = re.compile(r"[_-]?\d{8}(v\d+)?$")


class NoSymmetricInverse(Exception):
    """This API cannot be inverted mechanically. Carries the reason for the operator."""


def classify_event(event_name: str) -> Tuple[bool, Optional[str]]:
    """Can this operation be inverted by re-calling it with the old value?

    Returns ``(invertible, reason_if_not)``.
    """
    event_name = strip_api_version(event_name)
    if event_name.startswith(LIFECYCLE_PREFIXES):
        return False, (
            "%s creates, destroys or moves a resource; there is no symmetric inverse and "
            "CloudTrail does not record everything a re-creation would need" % event_name
        )
    if event_name.startswith(WHOLE_DOCUMENT_PREFIXES):
        return False, (
            "%s replaces a whole document rather than merging fields; calling it with only "
            "this field would discard the rest" % event_name
        )
    if event_name.startswith(PARTIAL_UPDATE_PREFIXES):
        return True, None
    return False, (
        "%s is not a recognised partial-update operation, so re-calling it with the old "
        "value is not safe to assume" % event_name
    )


def service_from_source(event_source: str) -> str:
    """``ec2.amazonaws.com`` -> ``ec2``, which is also the AWS CLI service name."""
    return (event_source or "").split(".")[0]


def flag_segment(path: Tuple[str, ...]) -> Optional[str]:
    """The path segment that names the CLI flag, ignoring structural wrappers.

    ``("disableApiTermination", "value")`` -> ``disableApiTermination``.
    """
    meaningful = [s for s in path if s.lower() not in WRAPPER_SEGMENTS]
    return meaningful[-1] if len(meaningful) == 1 else None


def command_is_derivable(path: Tuple[str, ...], value: str) -> Optional[str]:
    """Can a correct command be written for this path and value? If not, say why.

    A command that looks right but fails is worse than no command, so anything whose flag
    name cannot be derived with confidence is refused rather than guessed at.
    """
    if not path:
        return "the value this call set is not visible in requestParameters"
    if flag_segment(path) is None:
        return (
            "the parameter is nested (%s), and the CLI flag name for a nested field cannot "
            "be derived reliably from the CloudTrail path" % ".".join(path)
        )
    if LIST_SEPARATOR in value:
        return (
            "the value is a list (%s); the CLI spelling for list parameters varies by API, "
            "so the command is not guessed at" % value
        )
    return None


def cli_command(
    event_source: str,
    event_name: str,
    identifier_params: Dict[str, str],
    path: Tuple[str, ...],
    value: str,
    region: str = "",
) -> str:
    """An ``aws`` command a human can read, check and run.

    Deliberately the AWS CLI rather than boto3: it is what an operator will actually paste
    into a terminal during an incident, and it makes the call reviewable at a glance.

    Booleans become the flag pair the CLI actually accepts: ``--no-multi-az``, never
    ``--multi-az false``, which the CLI rejects.
    """
    service = service_from_source(event_source)
    parts: List[str] = ["aws", service, _kebab(event_name)]  # version suffix stripped
    for key, identifier in sorted(identifier_params.items()):
        parts += ["--%s" % _kebab(key), shlex.quote(identifier)]

    flag = _kebab(flag_segment(path) or path[-1])
    lowered = value.strip().lower()
    if lowered in BOOLEAN_VALUES:
        parts.append("--%s" % (flag if BOOLEAN_VALUES[lowered] else "no-" + flag))
    else:
        parts += ["--%s" % flag, shlex.quote(value)]
    if region:
        parts += ["--region", region]
    return " ".join(parts)


def strip_api_version(event_name: str) -> str:
    return _API_VERSION_SUFFIX.sub("", event_name)


def _kebab(name: str) -> str:
    """``ModifyDBInstance`` -> ``modify-db-instance``, ``multiAZ`` -> ``multi-az``.

    CloudTrail lowercases the leading letter of a parameter name (``dBInstanceIdentifier``
    for the API's ``DBInstanceIdentifier``), so the first character is restored before
    splitting or the leading capital run would be broken up.
    """
    text = strip_api_version(name).replace("_", "-")
    if text:
        text = text[0].upper() + text[1:]
    text = _BEFORE_WORD.sub(r"\1-\2", text)
    text = _AFTER_LOWER.sub(r"\1-\2", text)
    return text.lower()


def symmetric_inverse(
    event_source: str,
    event_name: str,
    resource_id: str,
    identifier_params: Dict[str, str],
    path: Tuple[str, ...],
    target_value: str,
    region: str = "",
) -> Dict[str, Any]:
    """Describe a generic inverse, or explain why there is not one.

    Never returns something executable: ``executable`` is always False here, and
    ``manual`` carries the command for a human. Execution is a plugin's privilege.
    """
    invertible, reason = classify_event(event_name)
    if not invertible:
        return {"executable": False, "manual": None, "reason": reason}
    undecidable = command_is_derivable(path, target_value)
    if undecidable is not None:
        return {
            "executable": False,
            "manual": None,
            "targetValue": target_value,
            "reason": "the previous value is known, but no command can be written: %s"
            % undecidable,
        }
    return {
        "executable": False,
        "targetValue": target_value,
        "manual": cli_command(
            event_source, event_name, identifier_params, path, target_value, region
        ),
        "api": "%s:%s" % (service_from_source(event_source), event_name),
        "reason": "no plugin vouches for this field, so the tool will not call AWS. The "
        "command above restores the recorded value; check it before running it.",
        "caveats": [
            "the tool cannot read this field's current value, so it cannot tell you "
            "whether somebody else has changed it since",
            "any precondition the API has (stopping an instance, for example) is not "
            "handled",
        ],
    }
