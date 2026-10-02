"""Deciding what "the window I'm looking at" means.

When the user types "look at this" into a terminal or IDE, *that* window has
focus - so the answer is usually the window they were in just before. Order of
evidence: the active window if it isn't the chat itself; else the most recent
focus change to another window; else the front-most other window. The window
under the mouse is offered as an alternative."""

from __future__ import annotations

from dataclasses import dataclass, field

from .desktop import Desktop, Window


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


@dataclass
class Choice:
    window: Window | None
    reason: str
    alternatives: list[tuple[str, Window]] = field(default_factory=list)
    host: Window | None = None  # the chat window that was skipped


def _usable(w: Window | None) -> bool:
    return w is not None and w.is_app and not w.host and not w.minimized


def host_label(desk: Desktop) -> str:
    hosts = [w for w in desk.windows if w.host]
    if hosts:
        return hosts[0].app or hosts[0].process
    return desk.host.app or "this chat"


def choose(desk: Desktop, history: list[tuple[float, int]]) -> Choice:
    """Pick the window the user most likely means. *history* is (time, hwnd)
    focus changes, newest first."""
    fg = desk.foreground_window
    host_window = fg if fg is not None and fg.host else None
    chat = host_label(desk)
    chosen: Window | None = None
    reason = ""

    if _usable(fg):
        chosen, reason = fg, "it's the active window"
    else:
        for t, hwnd in history:
            w = desk.window(hwnd)
            if _usable(w):
                when = ago(desk.taken - t)
                if host_window is not None:
                    reason = f"it's the window you were using before switching to {chat} ({when} ago)"
                else:
                    reason = f"it's the window you used most recently ({when} ago)"
                chosen = w
                break
        if chosen is None:
            for w in desk.windows:
                if _usable(w):
                    chosen = w
                    reason = f"it's the front-most window behind {chat}" if host_window else "it's the front-most window"
                    break

    alternatives: list[tuple[str, Window]] = []
    seen = {chosen.hwnd} if chosen else set()
    under = desk.window_under_cursor
    if _usable(under) and under.hwnd not in seen:
        alternatives.append(("under your mouse", under))
        seen.add(under.hwnd)
    for _t, hwnd in history:
        w = desk.window(hwnd)
        if _usable(w) and w.hwnd not in seen:
            alternatives.append(("used recently", w))
            seen.add(w.hwnd)
        if len(alternatives) >= 3:
            break
    for w in desk.windows:
        if len(alternatives) >= 3:
            break
        if _usable(w) and w.hwnd not in seen:
            alternatives.append(("also open", w))
            seen.add(w.hwnd)
    return Choice(chosen, reason, alternatives, host_window)
