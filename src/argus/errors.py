"""Keeping error text clean in the work profile.

Argus's own errors (ArgusError) are written to be safe: windows are named via
``privacy.display_name``, never by a hidden title. Unexpected exceptions are not:
their messages can carry anything (a window title in a KeyError, a capture's
file name, ...). In the work profile they're replaced by a generic message with
an error id, and the log gets the exception type, the id and the stack frames
(file, line and function only) - never the exception's message. Paths inside Argus's
data folder are scrubbed from every error, since capture file names are built
from window labels."""

from __future__ import annotations

import logging
import re
import secrets
import traceback

from .config import home, settings


def work_profile() -> bool:
    """Is the work profile on? Assume so if the settings can't be read."""
    try:
        return settings().work
    except Exception:  # noqa: BLE001 - failing closed is the point
        return True


# File types Argus keeps in its data folder. A path is scrubbed up to the last
# of these on its line (or to the end of the line), so a file name with spaces,
# commas or brackets - "Scan [Doe, John] 2026.png" - goes with it.
_DATA_EXTS = r"(?:png|jpe?g|jsonl|json|toml|log|key|txt)"


def _data_path_pattern() -> re.Pattern:
    parts = [p for p in re.split(r"[\\/]+", str(home())) if p]
    sep = r"[\\/]+"
    rest = rf"(?:[\\/][^\r\n]*\.{_DATA_EXTS}(?:\.(?:part|tmp))?(?![A-Za-z0-9])|[\\/][^\r\n]*)?"
    return re.compile(sep.join(re.escape(p) for p in parts) + rest, re.IGNORECASE)


def scrub(text: str) -> str:
    """*text* without paths into Argus's data folder (work profile only): the
    folder and everything after it up to the last known file extension on the
    line, or the rest of the line. Over-scrubbing beats leaking a file name."""
    if not work_profile():
        return text
    try:
        return _data_path_pattern().sub("<Argus data folder>", text)
    except Exception:  # noqa: BLE001
        return "Argus hit an error (details withheld by the work profile)."


def log_unexpected(log: logging.Logger, exc: BaseException, where: str) -> str | None:
    """Log *exc*. Returns an error id in the work profile (where only the type,
    the id and the stack frames are logged), else None after a normal
    ``log.exception``-style record."""
    if not work_profile():
        log.error("%s failed", where, exc_info=exc)
        return None
    eid = secrets.token_hex(4)
    # Code locations only: no exception message and no source text.
    frames = "".join(f"  {f.filename}:{f.lineno} in {f.name}\n" for f in traceback.extract_tb(exc.__traceback__))
    log.error("%s failed: %s [error id %s] (message withheld: work profile)\n%s", where, type(exc).__name__, eid, frames)
    return eid


def describe_unexpected(exc: BaseException, log: logging.Logger | None = None, where: str = "argus") -> str | None:
    """A message for the AI about an unexpected exception in the work profile,
    or None in the personal profile (callers keep their usual report)."""
    if not work_profile():
        return None
    eid = log_unexpected(log or logging.getLogger("argus"), exc, where)
    return (
        f"Argus hit an unexpected error (error id {eid}). The work profile keeps the details out of this reply; "
        "they're in Argus's log on the user's PC under that id."
    )
