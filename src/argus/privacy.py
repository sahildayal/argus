"""Privacy controls: the pause switch, blacking out sensitive windows, and
deleting old captures."""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw

from .config import Settings, home
from .desktop import _SHELL_CLASSES, _SHELL_PROCESSES, ArgusError, Desktop, Paused, Window
from .geometry import Rect
from .imaging import load_font

# ------------------------------------------------------------------- pause


@dataclass(frozen=True)
class PauseState:
    paused: bool
    since: float | None = None
    until: float | None = None

    def describe(self) -> str:
        if not self.paused:
            return "active"
        if self.until:
            return f"paused until {time.strftime('%H:%M', time.localtime(self.until))}"
        return "paused"


def _pause_file():
    return home() / "paused.json"


def pause_state() -> PauseState:
    path = _pause_file()
    if not path.exists():
        return PauseState(False)
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    until = data.get("until")
    if until and time.time() >= until:
        path.unlink(missing_ok=True)
        return PauseState(False)
    return PauseState(True, data.get("since"), until)


def pause(minutes: float | None = None) -> PauseState:
    now = time.time()
    until = now + minutes * 60 if minutes else None
    _pause_file().write_text(json.dumps({"since": now, "until": until}), encoding="utf-8")
    return PauseState(True, now, until)


def resume() -> None:
    _pause_file().unlink(missing_ok=True)


def ensure_not_paused() -> None:
    state = pause_state()
    if state.paused:
        when = f" until {time.strftime('%H:%M', time.localtime(state.until))}" if state.until else ""
        raise Paused(
            f"Argus is paused by the user{when}, so screen captures are blocked. Tell the user; they can "
            "resume from the tray icon, the pause hotkey, or `argus resume`. Don't try to work around it."
        )


# ---------------------------------------------------------------- redaction

_NOTIFICATION_TITLES = ("new notification", "notification center", "notification centre")


def sensitive_reason(w: Window, s: Settings) -> str | None:
    """Why this window must be blacked out, or None. Never includes the title itself."""
    process = w.process.lower()
    if process in s.redact_apps:
        return w.app or w.process
    if s.redact_notifications and process == "shellexperiencehost.exe" and w.title.lower().startswith(_NOTIFICATION_TITLES):
        return "notification"
    for pattern in s.redact_titles:
        if pattern.search(w.title):
            return f"{w.app or w.process} (privacy list)"
    if process in s.browsers:
        for pattern in s.redact_browser_titles:
            if pattern.search(w.title):
                return f"{w.app or 'browser'} tab (privacy list)"
    return None


# ------------------------------------------------------------- work profile


def allowed(w: Window, s: Settings) -> bool:
    """May this window be seen at all? Always True in the personal profile; in
    the work profile only windows on the [work] allow lists are (the block list
    still applies on top - see `shown`). Shell surfaces (desktop, taskbar,
    notifications, Start) and windows of unknown programs never are."""
    if not s.work:
        return True
    process = w.process.lower()
    if not process or not w.pid or w.cls in _SHELL_CLASSES or process in _SHELL_PROCESSES:
        return False
    if not w.attributed or process == "applicationframehost.exe":
        return False  # a UWP frame whose app couldn't be pinned to one process
    if process in s.allow_apps:
        return True
    if process in s.browsers and any(p.search(w.title) for p in s.allow_browser_titles):
        return True
    return any(p.search(w.title) for p in s.allow_titles)


def shown(w: Window, s: Settings) -> bool:
    """Allowed and not on the block list: its pixels and title may leave Argus."""
    return allowed(w, s) and sensitive_reason(w, s) is None


def display_name(w: Window, s: Settings) -> str:
    """How Argus names a window to the AI (lists, captions, errors, logs). The
    title is left out whenever the window is hidden by either profile."""
    if not allowed(w, s):
        return f"{w.app or w.process or 'unknown app'} (hidden, work profile)"
    if sensitive_reason(w, s):
        return f"{w.app or w.process} (title hidden, privacy list)"
    return w.label


def check_window(w: Window, s: Settings) -> None:
    """Refuse to target a window the work profile doesn't allow."""
    if not allowed(w, s):
        raise ArgusError(
            f"{display_name(w, s)} isn't allowed by the user's work profile, so Argus won't capture it or share its "
            "title. Tell the user; only they can allow an app (under [work] in ~/.argus/config.toml). "
            "Don't try to work around it."
        )


WORK_HIDDEN = "everything outside the allowed apps (work profile)"
WORK_LABEL = "Hidden by Argus: work profile"


def _live(desk: Desktop) -> list[Window]:
    """Windows that can put pixels on screen: not minimized, and not provably
    invisible. See-through (click-through / layered) windows count: unless
    their constant alpha is 0, they may well be drawing something."""
    return [w for w in desk.layers if not w.minimized and not w.transparent]


def shown_windows(desk: Desktop, s: Settings) -> set[int]:
    """Handles of the windows whose pixels the work profile reveals: allowed,
    unblocked windows, plus menus/popups that can be attributed to one of them.

    - An owned popup is revealed only if its owner is revealed through an
      unbroken chain from an allowed window, belongs to the same process, and is
      in front of it. One hidden window anywhere in the chain hides it.
    - An unowned popup is revealed only if its process has an allowed window it
      overlaps and sits in front of, and that process shows no hidden windows at
      all (every Chrome window shares one process, so with a hidden Chrome
      window open an unowned popup could be the hidden window's).
    - A popup of a UWP frame host, or of a process Argus can't read, never is."""
    live = _live(desk)
    tops = {w.hwnd: w for w in live if shown(w, s)}
    chained = set(tops)  # revealed through an owner chain that starts at an allowed window
    out = set(tops)
    # Every app window that isn't shown counts, minimized ones too.
    hidden_pids = {w.app_process_id for w in desk.layers if w.is_app and not shown(w, s)}
    by_hwnd = {w.hwnd: w for w in desk.layers}
    for p in sorted(live, key=lambda w: -w.z):  # back to front: owners come before their popups
        if p.hwnd in out or p.is_app or p.see_through or not p.pid or not p.attributed:
            continue
        if p.cls in _SHELL_CLASSES or p.process.lower() in _SHELL_PROCESSES or p.process.lower() == "applicationframehost.exe":
            continue
        if sensitive_reason(p, s):
            continue
        if p.owner:
            owner = by_hwnd.get(p.owner)
            if (
                owner is not None
                and owner.hwnd in chained
                and owner.app_process_id == p.app_process_id
                and p.z < owner.z
            ):
                out.add(p.hwnd)
                chained.add(p.hwnd)
        elif p.app_process_id not in hidden_pids and any(
            t.app_process_id == p.app_process_id and p.z < t.z and t.rect.intersect(p.rect) for t in tops.values()
        ):
            out.add(p.hwnd)
    return out


# Windows 11 rounds the corners of normal windows (8 px at 100% scaling), so a
# window's frame rect includes a few corner pixels of whatever is behind it.
# Revealed windows are drawn as rounded rects with that radius, inset by 1 px,
# so those pixels stay hidden. Costs a sliver of the allowed window's corners.
CORNER_RADIUS = 8
REVEAL_INSET = 1


def _reveal(draw: ImageDraw.ImageDraw, w: Window, region: Rect, desk: Desktop) -> None:
    r = w.rect
    inset = 0 if w.maximized else REVEAL_INSET
    box = [r.x - region.x + inset, r.y - region.y + inset, r.right - region.x - 1 - inset, r.bottom - region.y - 1 - inset]
    if box[2] < box[0] or box[3] < box[1]:
        return
    mon = desk.monitor(w.monitor) if w.monitor else desk.monitor_for(r)
    radius = 0 if w.maximized else round(CORNER_RADIUS * (mon.dpi if mon else 96) / 96)
    radius = min(radius, (box[2] - box[0]) // 2, (box[3] - box[1]) // 2)
    if radius > 0:
        draw.rounded_rectangle(box, radius=radius, fill=0)
    else:
        draw.rectangle(box, fill=0)


def _redact_work(img: Image.Image, region: Rect, desk: Desktop, s: Settings) -> list[str]:
    """Allow-list redaction, failing closed: start fully hidden and paint windows
    back to front, revealing the shown ones and re-hiding whatever covers them.
    Wallpaper, taskbar, see-through overlays, any area no window covers and
    anything off the physical monitors stay hidden."""
    reveal = shown_windows(desk, s)
    mask = Image.new("L", img.size, 255)  # 255 = hidden
    draw = ImageDraw.Draw(mask)

    def local(r: Rect) -> list[int]:
        return [r.x - region.x, r.y - region.y, r.right - region.x - 1, r.bottom - region.y - 1]

    blocked: set[str] = set()
    for w in reversed(_live(desk)):
        inter = w.rect.intersect(region)
        if not inter:
            continue
        if w.hwnd in reveal and w.rect_exact:
            _reveal(draw, w, region, desk)
        else:
            draw.rectangle(local(inter), fill=255)
            if w.hwnd in reveal:
                blocked.add("a window whose exact position Windows didn't report")
            elif w.see_through:
                blocked.add("see-through overlay windows")
        if allowed(w, s) and (reason := sensitive_reason(w, s)):
            blocked.add(reason)
    # Only pixels on a physical monitor can belong to a window.
    monitors = Image.new("L", img.size, 255)
    mdraw = ImageDraw.Draw(monitors)
    for m in desk.monitors:
        if inter := m.rect.intersect(region):
            mdraw.rectangle(local(inter), fill=0)
    mask = ImageChops.lighter(mask, monitors)
    if not mask.getbbox():
        return []
    img.paste(SHADE, mask=mask)

    # One label per big, (nearly) solid hidden block - not one per sliver.
    candidates = [w.rect for w in desk.layers if not w.minimized and w.hwnd not in reveal]
    candidates += [m.rect for m in desk.monitors] + [region]
    placed: list[tuple[int, int, int, int]] = []
    for r in sorted(candidates, key=lambda r: -r.area):
        inter = r.intersect(region)
        if not inter or inter.w < 300 or inter.h < 120:
            continue
        box = (inter.x - region.x, inter.y - region.y, inter.right - region.x, inter.bottom - region.y)
        if any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in placed):
            continue
        if mask.crop(box).histogram()[255] < 0.9 * inter.area:
            continue
        if mask.getpixel(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)) != 255:
            continue
        _label(img, box, WORK_LABEL)
        placed.append(box)
        if len(placed) >= 8:
            break
    return [WORK_HIDDEN, *sorted(blocked)]


# ---------------------------------------------------------------- block list

SHADE = (24, 24, 27)


def redact(img: Image.Image, origin: tuple[int, int], desk: Desktop, s: Settings) -> list[str]:
    """Black out every sensitive window visible in *img* (which shows the desktop
    area starting at *origin*). Returns the reasons, one per window hidden. In
    the work profile, everything but the allowed windows is blacked out instead."""
    region = Rect(origin[0], origin[1], img.width, img.height)
    if s.work:
        return _redact_work(img, region, desk, s)
    hidden: list[str] = []
    for w in desk.layers:
        if w.minimized or w.see_through:
            continue
        reason = sensitive_reason(w, s)
        if not reason:
            continue
        inter = w.rect.intersect(region)
        if not inter:
            continue
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)

        def local(r: Rect) -> list[int]:
            return [r.x - region.x, r.y - region.y, r.right - region.x - 1, r.bottom - region.y - 1]

        draw.rectangle(local(inter), fill=255)
        # The app's own menus and dropdowns over it are just as private.
        for popup in desk.layers:
            if popup.z < w.z and popup.pid == w.pid and not popup.see_through:
                pi = popup.rect.intersect(region)
                if pi and popup.rect.intersect(w.rect):
                    draw.rectangle(local(pi), fill=255)
        # Other apps' windows in front of it stay visible.
        for other in desk.layers:
            if other.z >= w.z:
                break
            if other.see_through or other.pid == w.pid or other.cls in ("Progman", "WorkerW"):
                continue
            oi = other.rect.intersect(inter)
            if oi:
                draw.rectangle(local(oi), fill=0)
        box = mask.getbbox()
        if not box:
            continue
        img.paste(SHADE, mask=mask)
        _label(img, box, f"Hidden by Argus: {reason}")
        hidden.append(reason)
    return hidden


def _label(img: Image.Image, box: tuple[int, int, int, int], text: str) -> None:
    x0, y0, x1, y1 = box
    if x1 - x0 < 120 or y1 - y0 < 30:
        return
    draw = ImageDraw.Draw(img)
    font = load_font(max(12, min(22, (x1 - x0) // 28)))
    tw = draw.textlength(text, font=font)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    draw.text((cx - tw / 2, cy - font.size / 2), text, fill=(150, 150, 160), font=font)


# ---------------------------------------------------------------- retention

_cleanup_lock = threading.Lock()
_last_cleanup = 0.0


def cleanup(s: Settings, force: bool = False) -> int:
    """Delete captures and marks older than the retention period (at most hourly
    unless forced). Returns the number of files removed."""
    global _last_cleanup
    with _cleanup_lock:
        now = time.time()
        if not force and now - _last_cleanup < 3600:
            return 0
        _last_cleanup = now
    cutoff = now - s.retention_days * 86400
    removed = 0
    for folder in (home() / "captures", home() / "marks"):
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        for sub in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                sub.rmdir()  # only succeeds when empty
            except OSError:
                pass
    return removed


def purge_all() -> None:
    for folder in (home() / "captures", home() / "marks"):
        shutil.rmtree(folder, ignore_errors=True)
