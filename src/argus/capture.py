"""Grabbing pixels: whole monitors, regions, and windows (even partly covered ones)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import mss
from mss.exception import ScreenShotError
from PIL import Image

from . import win32
from .config import Settings
from .desktop import ArgusError, Desktop, Monitor, Window, find_monitors, find_windows
from .geometry import Rect
from .privacy import WindowSet, check_window, display_name, named_windows, redact, sensitive_reason, share_scope, shown, work_mask


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
    profile: str = ""  # privacy profile it was taken under ("" = unknown, e.g. any image file)
    share_windows: WindowSet | None = None  # live provenance, never loaded from a PNG
    policy: str = ""  # Settings.policy it was redacted under ("" = unknown or not authenticated)
    allow_trust: bool = True  # captions can name windows outside the pixel scope
    sealed: bool = False  # reopened from a PNG whose Argus seal checked out (its metadata is authentic)

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


# ---------------------------------------------------------- work-profile grabs

GRAB_ATTEMPTS = 3  # the first try plus two retries


class ScreenChanged(ArgusError):
    """The desktop kept changing while a work-profile capture was being taken."""


def _owners(w: Window, desk: Desktop, s: Settings) -> tuple:
    """*w*'s full owner chain as it bears on the work profile: each owner's
    handle and process, and for visible owners their position, state and verdict."""
    out = []
    chain = (w.owner_chain or ((w.owner, 0),)) if w.owner else ()
    for hwnd, pid in chain:
        o = desk.window(hwnd) if hwnd else None
        out.append((hwnd, pid) if o is None else (hwnd, pid, o.pid, o.app_pid, o.rect, o.z, o.minimized, shown(o, s)))
    return tuple(out)


def _layout(desk: Desktop, area: Rect, s: Settings) -> tuple:
    """Everything about *desk* that decides how *area* gets redacted in the work
    profile: the monitors, the foreground window and, front to back, every window
    that touches the area with its position, process, maximized state, full owner
    chain, transparency and allow/block verdict (which covers title changes
    that matter). Compared in addition to the mask itself (see `_grab_redacted`)."""
    windows = tuple(
        (w.hwnd, w.rect, w.pid, w.app_pid, w.process.lower(), w.owner, _owners(w, desk, s), w.cls, w.minimized,
         w.maximized, w.see_through, w.alpha, w.layered, w.colorkey, w.rect_exact, w.region, w.attributed, w.is_app,
         shown(w, s), sensitive_reason(w, s) is not None)
        for w in desk.layers
        if w.minimized or w.rect.intersect(area)
    )
    monitors = tuple((m.device, m.rect, m.dpi) for m in desk.monitors)
    # Processes with a hidden window anywhere decide whether their popups show.
    hidden_pids = frozenset(w.app_process_id for w in desk.layers if w.is_app and not shown(w, s))
    return monitors, desk.foreground, windows, hidden_pids


def _same_redaction(before: Desktop, after: Desktop, area: Rect, s: Settings) -> bool:
    """Would *area* be redacted pixel-for-pixel the same against both snapshots?
    The masks themselves are compared, so no field that matters can be missed."""
    if _layout(before, area, s) != _layout(after, area, s):
        return False
    mask_before, _, _ = work_mask(area, before, s)
    mask_after, _, _ = work_mask(area, after, s)
    return mask_before.tobytes() == mask_after.tobytes()


def _grab_redacted(desk: Desktop, s: Settings, area_of: Callable[[Desktop], Rect]) -> tuple[Image.Image, Rect, list[str], Desktop]:
    """Grab and redact the area *area_of(desk)*. Returns (image, area, hidden, the
    desktop it was redacted against).

    In the work profile the window list is read again right after the grab and
    the redaction mask is computed from both snapshots; unless the two masks are
    pixel-identical (and nothing else that decides redaction changed - a window
    opened, moved, changed z-order, owner, maximized state, title or focus), the
    grab is retried, and after GRAB_ATTEMPTS the capture is refused rather than
    risking a stale mask.

    Limitation: a window that appears *and* disappears entirely between the two
    snapshots (shown only while the pixels were being grabbed) can't be
    detected this way - both snapshots agree and neither has it. The approval
    gate's preview, which the user sees before anything leaves, is the
    backstop for that."""
    if not s.work:
        area = area_of(desk)
        img = grab(area)
        return img, area, redact(img, (area.x, area.y), desk, s), desk
    if not win32.dpi_ok():
        raise ArgusError(
            f"Argus couldn't make itself per-monitor DPI aware (it got {win32.DPI_AWARENESS!r}), so window positions "
            "may be scaled and the work profile can't redact reliably. Nothing was captured. Tell the user; restarting "
            "the AI tool usually fixes it."
        )
    if desk.refresh is None:
        raise ArgusError("This desktop snapshot can't be re-checked, so the work profile won't capture from it.")
    before = desk
    for _ in range(GRAB_ATTEMPTS):
        area = area_of(before)
        img = grab(area)
        after = before.refresh()
        if area_of(after) == area and _same_redaction(before, after, area, s):
            return img, area, redact(img, (area.x, area.y), after, s), after
        before = after
    raise ScreenChanged(
        "The screen kept changing while Argus was capturing it (windows opening, moving or switching), and the work "
        "profile won't risk a capture that may not match what's on screen. Nothing was captured; try again in a moment."
    )


def capture_monitor(desk: Desktop, mon: Monitor, s: Settings) -> Shot:
    def area_of(d: Desktop) -> Rect:
        m = next((m for m in d.monitors if m.device == mon.device), None)
        if m is None:
            raise ArgusError(f"{mon.title} was disconnected while Argus was capturing it.")
        return m.rect

    img, area, hidden, desk = _grab_redacted(desk, s, area_of)
    return Shot(
        image=img,
        kind="monitor",
        label=f"{mon.title}, {mon.name}",
        origin=(area.x, area.y),
        target={"monitor": str(mon.index), "device": mon.device},
        hidden=hidden,
        cursor=desk.cursor,
        monitor=mon,
        profile=s.profile,
        share_windows=share_scope(desk, s, area) if s.requires_approval else None,  # the grabbed area
        policy=s.policy,
    )


def capture_region(desk: Desktop, rect: Rect, s: Settings, label: str | None = None) -> Shot:
    if rect.empty:
        raise ArgusError("region needs a positive width and height: [x, y, width, height]")
    clipped = rect.intersect(desk.bounds)
    if clipped is None:
        raise ArgusError(f"Region {rect} is off every monitor. The desktop spans {desk.bounds}.")
    img, _, hidden, desk = _grab_redacted(desk, s, lambda d: clipped)
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
        profile=s.profile,
        share_windows=share_scope(desk, s, clipped) if s.requires_approval else None,
        policy=s.policy,
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


def _refuse_window(win: Window, s: Settings) -> None:
    check_window(win, s)  # work profile: refuse rather than return a black image
    if win.minimized:
        raise ArgusError(
            f"{display_name(win, s)} is minimized, so there's nothing on screen to capture. Ask the user to restore it, "
            "or capture a monitor instead."
        )
    reason = sensitive_reason(win, s)
    if reason:
        raise ArgusError(
            f"That window ({reason}) is on the user's privacy list, so Argus won't capture it. "
            "The user can change the list in ~/.argus/config.toml."
        )


def capture_window(desk: Desktop, win: Window, s: Settings) -> Shot:
    _refuse_window(win, s)
    mon = desk.monitor(win.monitor) if win.monitor else None
    base = Shot(
        image=Image.new("RGB", (1, 1)),
        kind="window",
        label=display_name(win, s) + (f" on {mon.title}" if mon else ""),
        origin=None,
        target={"window": win.hwnd, "title": win.title, "process": win.process},
        cursor=desk.cursor,
        monitor=mon,
        window=win,
        profile=s.profile,
        policy=s.policy,
    )
    visible = desk.visible_fraction(win)

    def area_of(d: Desktop) -> Rect:
        """The window plus its open menus, on screen. In the work profile the
        window is re-checked against every fresh snapshot too."""
        w = d.window(win.hwnd)
        if w is None:
            raise ArgusError(f"{display_name(win, s)} closed while Argus was capturing it.")
        _refuse_window(w, s)
        full = w.rect
        if s.work or visible >= 0.995:
            for popup in d.same_app_popups(w):  # open menus spill past the frame
                full = full.union(popup.rect)
        area = full.intersect(d.bounds)
        if area is None:
            raise ArgusError(f"{display_name(win, s)} is entirely off-screen.")
        return area

    if visible >= 0.995 or s.work:
        # The work profile never uses PrintWindow: an offscreen render shows the
        # whole window, so whatever covers it would have to be trusted to be
        # nothing. Covered parts are simply blacked out instead.
        base.image, area, base.hidden, final = _grab_redacted(desk, s, area_of)
        base.origin = (area.x, area.y)
        if s.requires_approval:  # trust what the pixels came from, not the first snapshot
            base.share_windows = share_scope(final, s, area, target=final.window(win.hwnd) or win)
        if visible < 0.995:
            covered = round((1 - visible) * 100)
            base.notes.append(
                f"About {covered}% of this window is covered by other windows or off-screen; the work profile blacks out "
                "whatever covers it instead of rendering the window offscreen."
            )
        return base

    rendered = render_window(win)
    if rendered is not None:
        base.image, base.method = rendered, "printwindow"
        base.origin = (win.rect.x, win.rect.y)
        if s.requires_approval:
            area = Rect(*base.origin, rendered.width, rendered.height)
            base.share_windows = share_scope(desk, s, area, target=win)
        covered = round((1 - visible) * 100)
        base.notes.append(
            f"About {covered}% of this window is covered or off-screen, so Argus had it render itself "
            "directly; open menus and tooltips aren't included."
        )
        return base

    area = win.rect.intersect(desk.bounds)
    if area is None:
        raise ArgusError(f"{display_name(win, s)} is entirely off-screen and refuses to render offscreen.")
    base.image = grab(area)
    base.origin = (area.x, area.y)
    base.hidden = redact(base.image, base.origin, desk, s)
    if s.requires_approval:
        base.share_windows = share_scope(desk, s, area, target=win)
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

        def name(w: Window) -> str:
            return display_name(w, s)

        # In the work profile, hidden windows can't be matched by title (no probing).
        matches = find_windows(desk, window, name=name, titled=(lambda w: shown(w, s)) if s.work else None)
        win = matches[0]
        check_window(win, s)
        if rect is not None:
            mon = desk.monitor(win.monitor) if win.monitor else None
            shot = capture_region(desk, rect.offset(win.rect.x, win.rect.y), s, label=f"part of {name(win)}")
            shot.window, shot.monitor = win, mon
        else:
            shot = capture_window(desk, win, s)
        others = [w for w in matches[1:4] if w.hwnd != win.hwnd]
        if others:
            shot.notes.append("Also matched: " + "; ".join(f"{name(w)} [{w.handle}]" for w in others) + ".")
        if shot.share_windows is not None:
            shot.allow_trust = named_windows([win, *others], s) <= shot.share_windows
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
            # The app restarted: find the same program with the closest title. In
            # the work profile only windows it shows are candidates, so a hidden
            # window's title is never compared (or picked).
            candidates = [
                w for w in desk.windows
                if w.process.lower() == str(target.get("process", "")).lower() and (not s.work or shown(w, s))
            ]
            if not candidates:
                what = "That window" if s.work else f'The window "{target.get("title", "?")}"'
                which = "allowed " if s.work else ""
                raise ArgusError(f"{what} is gone and no {which}{target.get('process')} window is open.")
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
