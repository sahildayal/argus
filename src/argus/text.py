"""Formatting for request arguments echoed back to the user or the AI."""

from unicodedata import category

_ARG_LIMIT = 200


def display_arg(value: str) -> str:
    """One bounded line, without control characters or invisible formatting."""
    cleaned = "".join(
        " " if ch.isspace() else ch
        for ch in value
        if ch.isspace() or (
            category(ch) not in {"Cc", "Cf", "Cs"}
            and ch != "\u034f"
            and not ("\ufe00" <= ch <= "\ufe0f" or "\U000e0100" <= ch <= "\U000e01ef")
        )
    )
    line = " ".join(cleaned.split())
    return line if len(line) <= _ARG_LIMIT else line[:_ARG_LIMIT - 1].rstrip() + "…"
