"""Formatting for request arguments echoed back to the user or the AI."""

from unicodedata import category, combining, normalize

_ARG_LIMIT = 200
_MAX_MARKS = 2  # combining marks kept after each base character
# Default-ignorable or blank-looking characters outside the Cf category.
_INVISIBLE = frozenset("͏ᅟᅠ឴឵⠀ㅤﾠ")


def _hidden(ch: str) -> bool:
    return (
        category(ch) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        or ch in _INVISIBLE
        or "᠋" <= ch <= "᠏"
        or "︀" <= ch <= "️"
        or "\U000e0100" <= ch <= "\U000e01ef"
    )


def display_arg(value: str) -> str:
    """One bounded line, without control characters, invisible formatting or mark floods."""
    out: list[str] = []
    marks = 0
    for ch in normalize("NFC", str(value)):
        if ch.isspace():
            out.append(" ")
            marks = 0
        elif _hidden(ch):
            continue
        elif combining(ch):
            if marks < _MAX_MARKS and out:
                out.append(ch)
            marks += 1
        else:
            out.append(ch)
            marks = 0
    line = " ".join("".join(out).split())
    return line if len(line) <= _ARG_LIMIT else line[:_ARG_LIMIT - 1].rstrip() + "…"
