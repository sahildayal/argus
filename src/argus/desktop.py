"""A snapshot of the desktop: monitors, windows front to back, the mouse, the
focused window, and which window hosts the AI session that is asking."""

from __future__ import annotations

import difflib
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import PurePath

import psutil
from PIL import Image, ImageDraw

from . import win32
from .geometry import Rect, bounding
from .text import display_arg
from .layout import arrange


class ArgusError(Exception):
    """A failure the AI should read and act on (bad selector, paused, minimized...)."""


class Paused(ArgusError):
    """The user has paused Argus; nothing may be captured."""


class PolicyChanged(ArgusError):
    """The privacy profile or its lists changed during a multi-frame call."""


# ----------------------------------------------------------------- monitors


@dataclass(frozen=True)
class Monitor:
    index: int  # display number, 1 = leftmost of the main row
    device: str  # \\.\DISPLAY1
    model: str  # EDID name, may be empty
    builtin: bool
    primary: bool
    rect: Rect
    work: Rect
    dpi: int
    position: str  # "left", "center", "bottom", ... ("" when it's the only monitor)

    @property
    def scale(self) -> int:
        return round(self.dpi * 100 / 96)

    @property
    def portrait(self) -> bool:
        return self.rect.h > self.rect.w

    @property
    def name(self) -> str:
        if self.builtin:
            return "Laptop screen" + (f" ({self.model})" if self.model else "")
        return self.model or "External monitor"

    @property
    def title(self) -> str:
        return f"Monitor {self.index}" + (f" ({self.position})" if self.position else "")

    def describe(self) -> str:
        bits = [self.title, self.name, f"{self.rect.w}x{self.rect.h} at ({self.rect.x}, {self.rect.y})", f"{self.scale}% scaling"]
        if self.portrait:
            bits.append("portrait")
        if self.primary:
            bits.append("primary")
        return " | ".join(bits)


# ------------------------------------------------------------------ windows

# Shell surfaces that are never "the window you're looking at".
_SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Input.InputSite.WindowClass",
    "XamlExplorerHostIslandWindow", "ForegroundStaging", "MultitaskingViewFrame", "TaskListThumbnailWnd",
}
_SHELL_PROCESSES = {
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe",
    "textinputhost.exe", "lockapp.exe", "shellhost.exe",
}


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    cls: str
    pid: int
    process: str  # exe file name, e.g. "chrome.exe"
    app: str  # friendly name, e.g. "Google Chrome"
    rect: Rect  # visible frame (no invisible resize borders)
    z: int  # position in the global front-to-back order (0 = frontmost)
    minimized: bool
    maximized: bool
    topmost: bool
    see_through: bool  # click-through or fully transparent overlay
    is_app: bool  # what Alt+Tab would show
    owner: int
    monitor: int | None  # display number holding most of the window
    host: bool = False  # the terminal/IDE this AI session runs in
    alpha: int | None = None  # constant alpha of a layered window (LWA_ALPHA), None if it has none
    rect_exact: bool = True  # False when the frame rect came from a fallback (may include borders)
    app_pid: int = 0  # the hosted app's process for UWP frames (ApplicationFrameHost), else 0
    attributed: bool = True  # False when the owning app couldn't be identified unambiguously
    layered: bool = False  # WS_EX_LAYERED: may be translucent or per-pixel alpha
    colorkey: bool = False  # a layered window that keys out one colour (see-through holes)
    # Every owner up the chain, nearest first: (HWND, PID), invisible owners
    # included. Empty means "only *owner* is known" (hand-made windows).
    owner_chain: tuple[tuple[int, int], ...] = ()
    region: tuple[int, Rect, bool] | None = None  # region type, bounds, covers full window

    @property
    def app_process_id(self) -> int:
        """The process the window's content belongs to (the UWP app, not its frame host)."""
        return self.app_pid or self.pid

    @property
    def transparent(self) -> bool:
        """Provably invisible: a layered window with constant alpha 0."""
        return self.alpha == 0

    @property
    def opaque(self) -> bool:
        """Whether the region fills the window and its pixels are provably opaque."""
        return (self.region is None or self.region[2]) and (
            not self.layered or (self.alpha == 255 and not self.colorkey)
        )

    @property
    def handle(self) -> str:
        return f"0x{self.hwnd:X}"

    @property
    def short_title(self) -> str:
        """Title without the trailing app name ("x - Google Chrome" -> "x")."""
        t = self.title.strip()
        for sep in (" - ", " — ", " – "):
            if self.app and t.endswith(sep + self.app):
                return t[: -len(sep + self.app)].strip() or t
        return t

    @property
    def label(self) -> str:
        t = self.short_title
        if len(t) > 80:
            t = t[:77] + "..."
        if not self.app or self.app.lower() in t.lower():
            return f'"{t}"'
        return f'{self.app} - "{t}"'


@dataclass(frozen=True)
class Host:
    """Where this AI session lives, so look() can skip it."""

    pids: frozenset[int]
    names: tuple[str, ...]
    exact: frozenset[int]  # specific windows known to host the session

    def owns(self, w: Window) -> bool:
        if self.exact:
            return w.hwnd in self.exact
        return w.pid in self.pids

    @property
    def app(self) -> str | None:
        gui = [n for n in self.names if n.lower() not in _CLI_HOSTS]
        return gui[-1] if gui else (self.names[-1] if self.names else None)


_STOP_PARENTS = {
    "explorer.exe", "services.exe", "svchost.exe", "wininit.exe", "winlogon.exe", "sihost.exe",
    "userinit.exe", "taskhostw.exe", "runtimebroker.exe", "system",
}
_CLI_HOSTS = {"python.exe", "pythonw.exe", "uv.exe", "uvx.exe", "node.exe", "cmd.exe", "conhost.exe", "argus-mcp.exe"}

_host_lock = threading.Lock()
_host: Host | None = None


def detect_host() -> Host:
    """Work out which windows belong to the program that launched us (Windows
    Terminal, VS Code, Claude Desktop...). Called once at server start, while the
    window you launched the session from is most likely still in front."""
    global _host
    with _host_lock:
        if _host is not None:
            return _host
        pids, names = set(), []
        try:
            proc = psutil.Process(os.getpid())
            for _ in range(16):
                proc = proc.parent()
                if proc is None:
                    break
                name = proc.name()
                if name.lower() in _STOP_PARENTS:
                    break
                pids.add(proc.pid)
                names.append(name)
        except psutil.Error:
            pass

        exact: set[int] = set()
        console = win32.hwnd_int(win32.GetConsoleWindow())
        if console:
            root_owner = win32.hwnd_int(win32.GetAncestor(console, win32.GA_ROOTOWNER))
            for h in (console, root_owner):
                if h and win32.IsWindowVisible(h):
                    exact.add(h)
        if not exact:
            fg = win32.hwnd_int(win32.GetForegroundWindow())
            if fg and win32.window_pid(fg) in pids:
                exact.add(fg)
        _host = Host(frozenset(pids), tuple(names), frozenset(exact))
        return _host


def set_host(host: Host) -> None:
    """Override host detection (tests, and the tray app which has no host)."""
    global _host
    with _host_lock:
        _host = host


NO_HOST = Host(frozenset(), (), frozenset())

# (pid, process start time) -> (time cached, exe name, exe path). Keyed by the
# start time too, so a PID Windows reuses for another program is never mistaken
# for the old one (the work profile allows windows by exe name).
_proc_cache: dict[tuple[int, float], tuple[float, str, str]] = {}


def _process(pid: int) -> tuple[str, str]:
    """(exe name, exe path); ("", "") when the process can't be read (gone,
    protected or elevated beyond reach) - the work profile never allows those."""
    try:
        p = psutil.Process(pid)
        key = (pid, p.create_time())
    except psutil.Error:
        return "", ""
    hit = _proc_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < 120:
        return hit[1], hit[2]
    name, exe = "", ""
    try:
        name = p.name()
        try:
            exe = p.exe()
        except psutil.Error:
            exe = ""
    except psutil.Error:
        return "", ""
    if len(_proc_cache) > 2048:
        _proc_cache.clear()
    _proc_cache[key] = (now, name, exe)
    return name, exe


def _friendly_app(hwnd: int, pid: int, process: str, exe: str) -> tuple[str, str, int, bool]:
    """(process, app name, pid, attributed) - looking through ApplicationFrameHost
    to the real UWP app. *attributed* is False when a frame host's app can't be
    pinned to exactly one process (the work profile then never allows it)."""
    attributed = True
    if process.lower() == "applicationframehost.exe":
        hosted = {}
        for child in win32.child_windows(hwnd):
            cpid = win32.window_pid(child)
            if cpid and cpid != pid and cpid not in hosted:
                hosted[cpid] = _process(cpid)
        named = [(cpid, name, path) for cpid, (name, path) in hosted.items() if name]
        attributed = len(hosted) == 1 and len(named) == 1
        if named:  # the personal profile's block list still uses the first match
            pid, process, exe = named[0]
    app = win32.file_description(exe) if exe else ""
    if process.lower() == "explorer.exe":
        app = "File Explorer"
    elif not app or app.lower() == "application frame host":
        app = PurePath(process).stem
    app = _APP_RENAMES.get(app, app)
    return process, app, pid, attributed


# File descriptions that read oddly as app names.
_APP_RENAMES = {"Windows Terminal Host": "Windows Terminal", "Console Window Host": "Console"}


# ------------------------------------------------------------------ desktop


@dataclass(frozen=True)
class Desktop:
    monitors: tuple[Monitor, ...]
    layers: tuple[Window, ...]  # every visible top-level window, front to back
    cursor: tuple[int, int]
    foreground: int
    under_cursor: int
    host: Host
    taken: float
    # Takes a new snapshot of the same desktop (the work profile checks that
    # nothing moved while the pixels were grabbed). None for hand-made desktops.
    refresh: Callable[[], Desktop] | None = field(default=None, compare=False, repr=False)

    @property
    def windows(self) -> list[Window]:
        """App windows (what Alt+Tab shows), front to back."""
        return [w for w in self.layers if w.is_app]

    @property
    def bounds(self) -> Rect:
        return bounding([m.rect for m in self.monitors]) or Rect(0, 0, 0, 0)

    def monitor(self, index: int) -> Monitor | None:
        return next((m for m in self.monitors if m.index == index), None)

    def monitor_at(self, x: float, y: float) -> Monitor | None:
        return next((m for m in self.monitors if m.rect.contains(x, y)), None)

    def monitor_for(self, rect: Rect) -> Monitor | None:
        best, best_area = None, 0
        for m in self.monitors:
            inter = rect.intersect(m.rect)
            if inter and inter.area > best_area:
                best, best_area = m, inter.area
        return best

    @property
    def cursor_monitor(self) -> Monitor:
        return self.monitor_at(*self.cursor) or self.monitors[0]

    def window(self, hwnd: int) -> Window | None:
        return next((w for w in self.layers if w.hwnd == hwnd), None)

    @property
    def foreground_window(self) -> Window | None:
        w = self.window(self.foreground)
        return w if w and w.is_app else None

    @property
    def window_under_cursor(self) -> Window | None:
        w = self.window(self.under_cursor)
        if w and not w.is_app and w.owner:
            w = self.window(w.owner) or w
        return w if w and w.is_app else None

    def occluders(self, target: Window) -> list[Window]:
        """Opaque windows in front of *target* that aren't part of the same app
        (its own menus, tooltips and dialogs count as part of what you see)."""
        out = []
        for w in self.layers:
            if w.z >= target.z:
                break
            if w.see_through or w.pid == target.pid or w.owner == target.hwnd:
                continue
            if w.cls in ("Progman", "WorkerW"):
                continue
            out.append(w)
        return out

    def same_app_popups(self, target: Window) -> list[Window]:
        """Menus/dropdowns of the target's app floating over it."""
        return [
            w
            for w in self.layers
            if w.z < target.z and not w.is_app and not w.see_through and w.pid == target.pid and w.rect.intersect(target.rect)
        ]

    def visible_fraction(self, target: Window) -> float:
        """How much of the window is on a monitor and not covered by another app."""
        r = target.rect
        if r.empty or target.minimized:
            return 0.0
        s = 8
        width, height = max(1, math.ceil(r.w / s)), max(1, math.ceil(r.h / s))
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)

        def box(inter: Rect) -> list[int]:
            return [
                (inter.x - r.x) // s,
                (inter.y - r.y) // s,
                math.ceil((inter.right - r.x) / s) - 1,
                math.ceil((inter.bottom - r.y) / s) - 1,
            ]

        for m in self.monitors:
            inter = r.intersect(m.rect)
            if inter:
                draw.rectangle(box(inter), fill=255)
        for w in self.occluders(target):
            inter = r.intersect(w.rect)
            if inter:
                draw.rectangle(box(inter), fill=0)
        return mask.histogram()[255] / (width * height)


def snapshot(host: Host | None = None) -> Desktop:
    raw = win32.monitors_raw()
    if not raw:
        raise ArgusError("Windows reports no active monitors (is the session locked or the display off?)")
    names = win32.display_names()
    primary = next((i for i, m in enumerate(raw) if m[4]), 0)
    order, labels = arrange([m[2] for m in raw], primary)
    monitors = []
    for number, i in enumerate(order, 1):
        _hmon, device, rect, work, is_primary, dpi = raw[i]
        model, tech = names.get(device, ("", -1))
        monitors.append(Monitor(number, device, model, tech in win32.BUILTIN_OUTPUT_TECH, is_primary, rect, work, dpi, labels[i]))

    host = host or detect_host()
    fg = win32.hwnd_int(win32.GetForegroundWindow())
    cursor = win32.cursor_pos()
    under = win32.root_window_at(*cursor)

    layers = []
    for z, h in enumerate(win32.top_level_windows()):
        if win32.GetPropW(h, win32.APPROVAL_PREVIEW):
            continue  # never list or PrintWindow an in-progress Argus approval
        if not win32.IsWindowVisible(h) or win32.is_cloaked(h):
            continue
        minimized = bool(win32.IsIconic(h))
        rect, exact = win32.frame_rect_ex(h)
        if rect.empty and not minimized:
            continue
        ex = win32.ex_style(h)
        title = win32.window_text(h)
        cls = win32.class_name(h)
        pid = win32.window_pid(h)
        process, exe = _process(pid)
        layered = bool(ex & win32.WS_EX_LAYERED)
        alpha, colorkey = win32.layered_attributes(h) if layered else (None, False)
        see_through = bool(ex & win32.WS_EX_TRANSPARENT) or alpha == 0
        own = win32.owner(h)
        chain = _owner_chain(own)
        is_app = (
            not see_through
            and cls not in _SHELL_CLASSES
            and process.lower() not in _SHELL_PROCESSES
            and bool(title.strip())
            and (minimized or (rect.w >= 40 and rect.h >= 30))
            and not (ex & win32.WS_EX_TOOLWINDOW and not ex & win32.WS_EX_APPWINDOW)
            and not (ex & win32.WS_EX_NOACTIVATE and not ex & win32.WS_EX_APPWINDOW)
        )
        app, app_pid, attributed = "", 0, True
        if is_app:
            process, app, resolved, attributed = _friendly_app(h, pid, process, exe)
            app_pid = resolved if resolved != pid else 0
        elif process.lower() == "applicationframehost.exe":
            attributed = False  # a frame host's popups can't be tied to one app
        mon = None if minimized else _monitor_for(monitors, rect)
        w = Window(
            hwnd=h, title=title, cls=cls, pid=pid, process=process, app=app, rect=rect, z=z,
            minimized=minimized, maximized=bool(win32.IsZoomed(h)), topmost=bool(ex & win32.WS_EX_TOPMOST),
            see_through=see_through, is_app=is_app, owner=own, monitor=mon,
            alpha=alpha, rect_exact=exact, app_pid=app_pid, attributed=attributed,
            layered=layered, colorkey=colorkey, owner_chain=chain, region=win32.window_region(h, rect),
        )
        layers.append(_with_host(w, host))
    return Desktop(tuple(monitors), tuple(layers), cursor, fg, under, host, time.time(), refresh=lambda: snapshot(host))


OWNER_CHAIN_LIMIT = 16


def _owner_chain(first: int) -> tuple[tuple[int, int], ...]:
    """(HWND, PID) of *first* and every owner above it. A chain that loops or is
    too deep ends with (0, 0), which no check accepts."""
    chain: list[tuple[int, int]] = []
    seen: set[int] = set()
    h = first
    while h:
        if h in seen or len(chain) >= OWNER_CHAIN_LIMIT:
            chain.append((0, 0))
            break
        seen.add(h)
        chain.append((h, win32.window_pid(h)))
        h = win32.owner(h)
    return tuple(chain)


def _monitor_for(monitors: list[Monitor], rect: Rect) -> int | None:
    best, best_area = None, 0
    for m in monitors:
        inter = rect.intersect(m.rect)
        if inter and inter.area > best_area:
            best, best_area = m.index, inter.area
    return best


def _with_host(w: Window, host: Host) -> Window:
    return replace(w, host=True) if host.owns(w) else w


# ---------------------------------------------------------------- selection

_MONITOR_ALIASES = {
    "primary": "primary", "main": "primary",
    "laptop": "builtin", "builtin": "builtin", "built-in": "builtin", "internal": "builtin",
    "cursor": "cursor", "mouse": "cursor", "pointer": "cursor",
    "active": "active", "focused": "active", "foreground": "active", "current": "active",
    "middle": "center", "centre": "center",
}


def find_monitors(desk: Desktop, spec: str | int) -> list[Monitor]:
    """Resolve a monitor selector to monitors. Raises ArgusError with the options
    when nothing (or more than one thing) matches."""
    text = str(spec).strip().lower()
    if text in ("all", "*", "every", "everything"):
        return list(desk.monitors)
    m = re.fullmatch(r"(?:monitor|screen|display)?\s*#?\s*(\d+)", text)
    if m:
        mon = desk.monitor(int(m.group(1)))
        if mon:
            return [mon]
        raise ArgusError(f"There is no monitor {display_arg(m.group(1))}. {_monitor_options(desk)}")
    text = re.sub(r"^the\s+", "", text)
    text = re.sub(r"^(monitor|screen|display)\s+", "", text)
    text = re.sub(r"\s+(monitor|screen|display)$", "", text)
    kind = _MONITOR_ALIASES.get(text, text)

    if kind == "primary":
        return [m for m in desk.monitors if m.primary][:1] or [desk.monitors[0]]
    if kind == "builtin":
        found = [m for m in desk.monitors if m.builtin]
        if found:
            return found[:1]
        raise ArgusError(f"No built-in (laptop) screen is active. {_monitor_options(desk)}")
    if kind == "cursor":
        return [desk.cursor_monitor]
    if kind == "active":
        fw = desk.foreground_window
        return [desk.monitor(fw.monitor)] if fw and fw.monitor else [desk.cursor_monitor]
    if kind in ("leftmost", "rightmost", "topmost", "bottommost"):
        key = {
            "leftmost": lambda m: m.rect.center[0],
            "rightmost": lambda m: -m.rect.center[0],
            "topmost": lambda m: m.rect.center[1],
            "bottommost": lambda m: -m.rect.center[1],
        }[kind]
        return [min(desk.monitors, key=key)]
    if kind in ("portrait", "vertical"):
        found = [m for m in desk.monitors if m.portrait]
        if len(found) == 1:
            return found
    if kind in ("landscape", "horizontal"):
        found = [m for m in desk.monitors if not m.portrait]
        if len(found) == 1:
            return found

    exact = [m for m in desk.monitors if m.position == kind]
    if exact:
        return exact
    if kind in ("left", "right", "center", "top", "bottom"):
        partial = [m for m in desk.monitors if kind in m.position.split("-")]
        if len(partial) == 1:
            return partial
        if len(partial) > 1:
            names = ", ".join(f'"{m.position}" ({m.title})' for m in partial)
            raise ArgusError(f'"{display_arg(str(spec))}" matches several monitors: {names}. Pick one.')
        if len(desk.monitors) == 1:
            return list(desk.monitors)
    device = [m for m in desk.monitors if m.device.lower().lstrip("\\.").endswith(text.lstrip("\\."))]
    if device and text.lstrip("\\.").startswith("display"):
        return device[:1]
    named = [m for m in desk.monitors if text and (text in m.model.lower() or text in m.name.lower())]
    if len(named) == 1:
        return named
    if len(named) > 1:
        raise ArgusError(f'"{display_arg(str(spec))}" matches several monitors. {_monitor_options(desk)}')
    raise ArgusError(f'No monitor matches "{display_arg(str(spec))}". {_monitor_options(desk)}')


def _monitor_options(desk: Desktop) -> str:
    opts = "; ".join(m.title + f" = {m.name}" for m in desk.monitors)
    return f"Monitors: {opts}. Also: all, primary, cursor, active" + (", laptop." if any(m.builtin for m in desk.monitors) else ".")


def find_windows(
    desk: Desktop,
    spec: str | int,
    *,
    name: Callable[[Window], str] | None = None,
    titled: Callable[[Window], bool] | None = None,
) -> list[Window]:
    """Windows matching a selector, best match first. *name* is how windows are
    named in error messages (default: their label); windows for which *titled*
    is False are matched by app, exe and handle only, never by title, so a
    selector can't be used to probe a hidden window's title."""
    text = str(spec).strip()
    low = text.lower()
    if low in ("active", "focused", "foreground", "current"):
        w = desk.foreground_window
        if w:
            return [w]
        raise ArgusError("No app window has focus right now (the desktop or taskbar is active).")
    if low in ("under_cursor", "under cursor", "mouse", "hover", "hovered", "pointer"):
        w = desk.window_under_cursor
        if w:
            return [w]
        raise ArgusError("The mouse isn't over an app window (it's over the desktop or taskbar).")
    if re.fullmatch(r"0x[0-9a-fA-F]+|\d{4,}", text):
        w = desk.window(int(text, 0))
        if w:
            return [w]
        raise ArgusError(f"No visible window has handle {display_arg(text)} (it may have closed). Call list_screens for current handles.")

    low = _APP_NICKNAMES.get(low, low)
    words = [t for t in re.split(r"\W+", low) if t]
    scored: list[tuple[float, Window]] = []
    for w in desk.windows:
        title = w.title.lower() if titled is None or titled(w) else ""
        app, proc = w.app.lower(), PurePath(w.process.lower()).stem
        haystack = f"{title} {app} {proc}"
        if title and title == low:
            score = 100.0
        elif low and title and low in title:
            score = 85.0
        elif low and (low in app or proc == low or proc.startswith(low)):
            score = 75.0
        elif words and all(word in haystack for word in words):
            score = 65.0
        else:
            ratio = max(
                (difflib.SequenceMatcher(None, low, token).ratio() for token in re.split(r"[\s\-|–—:]+", haystack) if token),
                default=0.0,
            )
            score = 40.0 * ratio if ratio >= 0.75 else 0.0
        if score:
            if w.minimized:
                score -= 5
            scored.append((score, w))
    scored.sort(key=lambda sw: (-sw[0], sw[1].z))
    if not scored:
        raise ArgusError(f'No window matches "{display_arg(text)}". Open windows: {_window_options(desk, name=name)}')
    return [w for _, w in scored]


# What people call apps vs. what their exe says they are.
_APP_NICKNAMES = {
    "vs code": "visual studio code", "vscode": "visual studio code", "terminal": "windows terminal",
    "explorer": "file explorer", "edge": "microsoft edge", "files": "file explorer", "cmd": "command prompt",
}


def _window_options(desk: Desktop, limit: int = 12, name: Callable[[Window], str] | None = None) -> str:
    wins = [w for w in desk.windows if not w.host][:limit]
    return "; ".join(f"{name(w) if name else w.label} [{w.handle}]" for w in wins) or "none"
