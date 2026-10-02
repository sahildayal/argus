"""Privacy controls: the pause switch, blacking out sensitive windows, and
deleting old captures."""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .config import Settings, home
from .desktop import Desktop, Paused, Window
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


SHADE = (24, 24, 27)


def redact(img: Image.Image, origin: tuple[int, int], desk: Desktop, s: Settings) -> list[str]:
    """Black out every sensitive window visible in *img* (which shows the desktop
    area starting at *origin*). Returns the reasons, one per window hidden."""
    region = Rect(origin[0], origin[1], img.width, img.height)
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
