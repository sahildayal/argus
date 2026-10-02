"""Grabbing pixels: whole monitors, regions, and windows (even partly covered ones)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import mss
from mss.exception import ScreenShotError
from PIL import Image

from . import win32
from .config import Settings
from .desktop import ArgusError, Desktop, Monitor, Window, find_monitors, find_windows
from .geometry import Rect
from .privacy import redact, sensitive_reason


@dataclass
class Shot:
    image: Image.Image  # full resolution, already redacted
    kind: str  # monitor | window | region | snip | mark | file | zoom | baseline
    label: str  # what it shows, for humans and the AI
    origin: tuple[int, int] | None  # desktop position of the image's top-left (None if unknown)
    target: dict  # how to capture the same thing again (baselines, wait_for)
    method: str = "screen"  # screen | printwindow | clipboard | file | crop
    notes: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)  # what privacy redaction blacked out
    cursor: tuple[int, int] | None = None  # desktop position of the mouse when taken
    monitor: Monitor | None = None
    window: Window | None = None
    taken: float = field(default_factory=time.time)
    source: Path | None = None  # the file a snip/mark came from

    @property
    def cursor_in_image(self) -> tuple[int, int] | None:
        if self.cursor is None or self.origin is None:
            return None
        x, y = self.cursor[0] - self.origin[0], self.cursor[1] - self.origin[1]
        if 0 <= x < self.image.width and 0 <= y < self.image.height:
            return (x, y)
        return None


def grab(rect: Rect) -> Image.Image:
    """Screen pixels inside *rect* (desktop coordinates)."""
    try:
        with mss.MSS() as sct:
            raw = sct.grab({"left": rect.x, "top": rect.y, "width": rect.w, "height": rect.h})
    except ScreenShotError as exc:
        raise ArgusError(
            f"Windows refused the screen capture ({exc}). That happens while the PC is locked, on the "
            "sign-in or UAC screen, or with the display off - try again once the desktop is back."
        ) from None
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def capture_monitor(desk: Desktop, mon: Monitor, s: Settings) -> Shot:
    img = grab(mon.rect)
    hidden = redact(img, (mon.rect.x, mon.rect.y), desk, s)
    return Shot(
        image=img,
        kind="monitor",
        label=f"{mon.title}, {mon.name}",
        origin=(mon.rect.x, mon.rect.y),
        target={"monitor": str(mon.index), "device": mon.device},
        hidden=hidden,
        cursor=desk.cursor,
        monitor=mon,
    )


def capture_region(desk: Desktop, rect: Rect, s: Settings, label: str | None = None) -> Shot:
    if rect.empty:
        raise ArgusError("region needs a positive width and height: [x, y, width, height]")
    clipped = rect.intersect(desk.bounds)
    if clipped is None:
        raise ArgusError(f"Region {rect} is off every monitor. The desktop spans {desk.bounds}.")
    img = grab(clipped)
    hidden = redact(img, (clipped.x, clipped.y), desk, s)
    notes = [] if clipped == rect else [f"Clipped to the screen: {clipped}."]
    mon = desk.monitor_for(clipped)
    return Shot(
        image=img,
        kind="region",
        label=label or f"region {clipped}" + (f" on {mon.title}" if mon else ""),
        origin=(clipped.x, clipped.y),
        target={"region": [clipped.x, clipped.y, clipped.w, clipped.h]},
        notes=notes,
        hidden=hidden,
        cursor=desk.cursor,
        monitor=mon,
    )


def _looks_blank(img: Image.Image) -> bool:
    w, h = img.size
    inner = img.crop((w // 10, h // 10, w - w // 10, h - h // 10)) if w > 40 and h > 40 else img
    return all(hi - lo <= 2 for lo, hi in inner.getextrema())


def render_window(win: Window) -> Image.Image | None:
    """Ask the window to paint itself offscreen - works when it's covered."""
    full = win32.window_rect(win.hwnd)
    result = win32.print_window(win.hwnd)
    if result is None:
        return None
    data, w, h = result
    img = Image.frombytes("RGB", (w, h), data, "raw", "BGRX")
    frame = win.rect
    left, top = frame.x - full.x, frame.y - full.y
    if 0 <= left < w and 0 <= top < h:
        img = img.crop((left, top, min(w, left + frame.w), min(h, top + frame.h)))
    return None if _looks_blank(img) else img


def capture_window(desk: Desktop, win: Window, s: Settings) -> Shot:
    if win.minimized:
        raise ArgusError(
            f"{win.label} is minimized, so there's nothing on screen to capture. Ask the user to restore it, "
            "or capture a monitor instead."
        )
    reason = sensitive_reason(win, s)
    if reason:
        raise ArgusError(
            f"That window ({reason}) is on the user's privacy list, so Argus won't capture it. "
            "The user can change the list in ~/.argus/config.toml."
        )
    mon = desk.monitor(win.monitor) if win.monitor else None
    base = Shot(
        image=Image.new("RGB", (1, 1)),
        kind="window",
        label=win.label + (f" on {mon.title}" if mon else ""),
        origin=None,
        target={"window": win.hwnd, "title": win.title, "process": win.process},
        cursor=desk.cursor,
        monitor=mon,
        window=win,
    )
    visible = desk.visible_fraction(win)
    if visible >= 0.995:
        area = win.rect
        for popup in desk.same_app_popups(win):  # open menus spill past the frame
            area = area.union(popup.rect)
        area = area.intersect(desk.bounds) or win.rect
        base.image = grab(area)
        base.origin = (area.x, area.y)
        base.hidden = redact(base.image, base.origin, desk, s)
        return base

    rendered = render_window(win)
    if rendered is not None:
        base.image, base.method = rendered, "printwindow"
        base.origin = (win.rect.x, win.rect.y)
        covered = round((1 - visible) * 100)
        base.notes.append(
            f"About {covered}% of this window is covered or off-screen, so Argus had it render itself "
            "directly; open menus and tooltips aren't included."
        )
        return base

    area = win.rect.intersect(desk.bounds)
    if area is None:
        raise ArgusError(f"{win.label} is entirely off-screen and refuses to render offscreen.")
    base.image = grab(area)
    base.origin = (area.x, area.y)
    base.hidden = redact(base.image, base.origin, desk, s)
    base.notes.append(
        "The window is partly covered and can't render itself offscreen, so this shows what's actually on "
        "screen there, including whatever is on top of it."
    )
    return base


def _parse_region(region) -> Rect:
    try:
        x, y, w, h = (int(round(float(v))) for v in region)
    except (TypeError, ValueError):
        raise ArgusError("region must be four numbers: [x, y, width, height]") from None
    return Rect(x, y, w, h)


def capture(desk: Desktop, s: Settings, monitor=None, window=None, region=None) -> list[Shot]:
    """Resolve the screenshot() style selectors into one or more shots. With
    ``region`` plus ``monitor``/``window``, the region is relative to that
    monitor/window's top-left corner."""
    if monitor is not None and window is not None:
        raise ArgusError("Give either monitor or window, not both.")
    rect = _parse_region(region) if region is not None else None

    if window is not None:
        matches = find_windows(desk, window)
        win = matches[0]
        if rect is not None:
            mon = desk.monitor(win.monitor) if win.monitor else None
            shot = capture_region(desk, rect.offset(win.rect.x, win.rect.y), s, label=f"part of {win.label}")
            shot.window, shot.monitor = win, mon
        else:
            shot = capture_window(desk, win, s)
        others = [w for w in matches[1:4] if w.hwnd != win.hwnd]
        if others:
            shot.notes.append("Also matched: " + "; ".join(f"{w.label} [{w.handle}]" for w in others) + ".")
        return [shot]

    if monitor is not None:
        mons = find_monitors(desk, monitor)
        if rect is not None:
            if len(mons) != 1:
                raise ArgusError("A region needs a single monitor (not 'all').")
            mon = mons[0]
            return [capture_region(desk, rect.offset(mon.rect.x, mon.rect.y), s, label=f"region {rect} of {mon.title}")]
        return [capture_monitor(desk, m, s) for m in mons]

    if rect is not None:
        return [capture_region(desk, rect, s)]
    return [capture_monitor(desk, m, s) for m in desk.monitors]


def recapture(desk: Desktop, s: Settings, target: dict) -> Shot:
    """Capture the same thing a previous shot showed (for baselines and waits)."""
    if "window" in target:
        win = desk.window(int(target["window"]))
        if win is None or not win.is_app:
            # The app restarted: find the same program with the closest title.
            candidates = [w for w in desk.windows if w.process.lower() == str(target.get("process", "")).lower()]
            if not candidates:
                raise ArgusError(f'The window "{target.get("title", "?")}" is gone and no {target.get("process")} window is open.')
            title = str(target.get("title", ""))
            win = max(candidates, key=lambda w: (_similar(w.title, title), -w.z))
        return capture_window(desk, win, s)
    if "region" in target:
        return capture_region(desk, Rect(*target["region"]), s)
    if "monitor" in target:
        mon = next((m for m in desk.monitors if m.device == target.get("device")), None)
        mon = mon or find_monitors(desk, target["monitor"])[0]
        return capture_monitor(desk, mon, s)
    raise ArgusError("That capture can't be re-taken (it came from a file, snip, or mark).")


def _similar(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
