"""The Argus tray app: global hotkeys to mark what you're looking at and to pause
Argus, plus a tray icon showing whether captures are allowed.

Run it with `argus-tray` (no console window) or `argus tray`."""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw

from . import privacy, win32
from .capture import capture_monitor, capture_window
from .config import config_path, home, parse_hotkey, pretty_hotkey, settings
from .desktop import NO_HOST, ArgusError, set_host, snapshot
from .geometry import Rect
from .marks import marks_dir, save_mark

log = logging.getLogger("argus.tray")

ACCENT = "#00D1FF"
WARN = "#FF4D4D"


def icon_image(paused: bool) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    outline = (150, 150, 160, 255) if paused else (235, 240, 245, 255)
    iris = (120, 120, 130, 255) if paused else (0, 209, 255, 255)
    d.ellipse([4, 16, 60, 48], outline=outline, width=5)  # the eye
    d.ellipse([22, 22, 42, 42], fill=iris)
    d.ellipse([28, 28, 36, 36], fill=(15, 15, 20, 255))
    if paused:
        d.line([10, 54, 54, 10], fill=(255, 77, 77, 255), width=7)
    return img


class Hotkeys(threading.Thread):
    """Registers global hotkeys and forwards presses to the UI queue."""

    def __init__(self, events: queue.Queue, bindings: dict[int, tuple[str, str]]):
        super().__init__(name="argus-hotkeys", daemon=True)
        self.events = events
        self.bindings = bindings  # id -> (action, spec)
        self.thread_id = 0

    def run(self) -> None:
        self.thread_id = win32.GetCurrentThreadId()
        failed = []
        for hid, (action, spec) in self.bindings.items():
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as exc:
                failed.append(f"{spec} ({exc})")
                continue
            if not win32.RegisterHotKey(None, hid, mods, vk):
                failed.append(f"{pretty_hotkey(spec)} (another app already uses it)")
        self.events.put(("hotkeys", failed))
        msg = wintypes.MSG()
        while win32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == win32.WM_HOTKEY and msg.wParam in self.bindings:
                self.events.put((self.bindings[msg.wParam][0], None))
        for hid in self.bindings:
            win32.UnregisterHotKey(None, hid)

    def stop(self) -> None:
        if self.thread_id:
            win32.PostThreadMessageW(self.thread_id, win32.WM_QUIT, 0, 0)


class TrayApp:
    def __init__(self) -> None:
        set_host(NO_HOST)  # the tray marks whatever you point at, including terminals
        self.events: queue.Queue = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        s = settings()
        self.hotkeys = Hotkeys(self.events, {1: ("mark", s.hotkey_mark), 2: ("pause", s.hotkey_pause)})
        self.paused = privacy.pause_state().paused
        self.icon = pystray.Icon("argus", icon_image(self.paused), self._title(), menu=self._menu())
        self._last_cleanup = 0.0

    # -- tray icon ---------------------------------------------------------------

    def _title(self) -> str:
        s = settings()
        state = privacy.pause_state().describe()
        return f"Argus: {state}\nMark: {pretty_hotkey(s.hotkey_mark)} | Pause: {pretty_hotkey(s.hotkey_pause)}"

    def _menu(self) -> pystray.Menu:
        s = settings()
        post = self.events.put
        return pystray.Menu(
            pystray.MenuItem(f"Mark: {pretty_hotkey(s.hotkey_mark)} over a window", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause captures", lambda: post(("pause", None)), checked=lambda _i: self.paused),
            pystray.MenuItem("Pause for 15 minutes", lambda: post(("pause15", None)), enabled=lambda _i: not self.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open captures folder", lambda: post(("open", home() / "captures"))),
            pystray.MenuItem("Open marks folder", lambda: post(("open", marks_dir()))),
            pystray.MenuItem("Edit settings", lambda: post(("open", config_path()))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Argus tray", lambda: post(("quit", None))),
        )

    def _refresh(self) -> None:
        paused = privacy.pause_state().paused
        if paused != self.paused:
            self.paused = paused
            self.icon.icon = icon_image(paused)
        self.icon.title = self._title()
        self.icon.update_menu()

    # -- loop ----------------------------------------------------------------------

    def run(self) -> None:
        self.hotkeys.start()
        self.icon.run_detached()
        self.root.after(50, self._poll)
        self.root.after(1000, self._tick)
        log.info("tray started")
        self.root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                action, arg = self.events.get_nowait()
                self._handle(action, arg)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _tick(self) -> None:
        try:
            self._refresh()
            if time.time() - self._last_cleanup > 3600:
                self._last_cleanup = time.time()
                privacy.cleanup(settings(), force=True)
        except Exception:
            log.exception("tick failed")
        self.root.after(1000, self._tick)

    def _handle(self, action: str, arg) -> None:
        try:
            if action == "mark":
                self.mark()
            elif action == "pause":
                self.toggle_pause()
            elif action == "pause15":
                privacy.pause(15)
                self._refresh()
                self.pill("Argus paused for 15 minutes: AI tools can't see your screens", WARN)
            elif action == "open":
                os.startfile(arg)  # noqa: S606 - opening our own folders/files
            elif action == "hotkeys" and arg:
                self.pill("Argus couldn't register: " + "; ".join(arg), WARN, ms=4000)
                log.warning("hotkeys failed: %s", arg)
            elif action == "quit":
                self.hotkeys.stop()
                self.icon.stop()
                self.root.quit()
        except Exception:
            log.exception("action %s failed", action)
            self.pill(f"Argus: {action} failed (see logs)", WARN)

    # -- actions -------------------------------------------------------------------

    def mark(self) -> None:
        s = settings()
        if privacy.pause_state().paused:
            self.pill("Argus is paused, so nothing was captured", WARN)
            return
        desk = snapshot(NO_HOST)
        win = desk.window_under_cursor or desk.foreground_window
        mon = desk.cursor_monitor
        window_shot, problem = None, ""
        if win is not None:
            try:
                window_shot = capture_window(desk, win, s)
            except ArgusError as exc:
                problem = str(exc)
        monitor_shot = capture_monitor(desk, mon, s)
        save_mark(window_shot, monitor_shot, extra={"problem": problem} if problem else None)
        if window_shot is not None and win is not None:
            self.flash(win.rect, f"Marked for Argus: {win.app or win.process}")
        elif problem:
            self.flash(mon.rect, f"Marked {mon.title} - that window is on your privacy list", colour=WARN)
        else:
            self.flash(mon.rect, f"Marked {mon.title}")
        log.info("mark: %s", win.label if window_shot and win else mon.title)

    def toggle_pause(self) -> None:
        if privacy.pause_state().paused:
            privacy.resume()
            self._refresh()
            self.pill("Argus resumed: AI tools can see your screens again", ACCENT)
        else:
            privacy.pause()
            self._refresh()
            self.pill("Argus paused: AI tools can't see your screens", WARN)

    # -- on-screen feedback (drawn after capturing, so never in a capture) ---------

    def _overlay(self, rect: Rect) -> tuple[tk.Toplevel, tk.Canvas]:
        key = "#010203"
        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=key)
        top.attributes("-transparentcolor", key)
        top.geometry(f"{rect.w}x{rect.h}+{rect.x}+{rect.y}")
        canvas = tk.Canvas(top, width=rect.w, height=rect.h, bg=key, highlightthickness=0)
        canvas.pack()
        top.update_idletasks()
        hwnd = win32.hwnd_int(win32.GetAncestor(top.winfo_id(), win32.GA_ROOT))
        if hwnd:  # click-through, never takes focus, not in Alt+Tab
            ex = win32.ex_style(hwnd)
            flags = win32.WS_EX_LAYERED | win32.WS_EX_TRANSPARENT | win32.WS_EX_NOACTIVATE | win32.WS_EX_TOOLWINDOW
            win32.SetWindowLongW(hwnd, win32.GWL_EXSTYLE, ctypes.c_long(ex | flags).value)
        return top, canvas

    def flash(self, rect: Rect, text: str, colour: str = ACCENT, ms: int = 700) -> None:
        top, c = self._overlay(rect)
        c.create_rectangle(2, 2, rect.w - 3, rect.h - 3, outline=colour, width=5)
        c.create_rectangle(10, 10, 10 + 9 * len(text) + 24, 44, fill="#16161a", outline=colour, width=2)
        c.create_text(22, 27, text=text, anchor="w", fill="white", font=("Segoe UI", 11, "bold"))
        top.after(ms, top.destroy)

    def pill(self, text: str, colour: str = ACCENT, ms: int = 1600) -> None:
        mon = snapshot(NO_HOST).cursor_monitor.rect
        w, h = 10 * len(text) + 48, 46
        rect = Rect(mon.x + (mon.w - w) // 2, mon.y + 60, w, h)
        top, c = self._overlay(rect)
        c.create_rectangle(1, 1, w - 2, h - 2, fill="#16161a", outline=colour, width=2)
        c.create_text(w // 2, h // 2, text=text, fill="white", font=("Segoe UI", 11, "bold"))
        top.after(ms, top.destroy)


def _setup_logging() -> None:
    logs = home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "tray.log", maxBytes=500_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("argus")
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main() -> None:
    _setup_logging()
    lock = win32.single_instance("Local\\ArgusTray")
    if lock is None:
        log.info("tray already running; exiting")
        return
    TrayApp().run()


if __name__ == "__main__":
    main()
