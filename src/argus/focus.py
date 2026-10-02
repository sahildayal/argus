"""Background watcher: which windows had focus recently, and when the clipboard
last changed. It's what lets look() answer "the window I was just in"."""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from ctypes import wintypes

from . import win32


class Tracker:
    def __init__(self, history: int = 200):
        self._history: deque[tuple[float, int]] = deque(maxlen=history)
        self.clip_seq: int | None = None
        self.clip_time: float | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        # ctypes callbacks must outlive the hooks that use them.
        self._event_proc = win32.WINEVENTPROC(self._on_event)
        self._wnd_proc = win32.WNDPROC(self._on_message)

    def start(self) -> Tracker:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="argus-tracker", daemon=True)
            self._thread.start()
            self._ready.wait(3)
        return self

    def stop(self) -> None:
        if self._thread_id:
            win32.PostThreadMessageW(self._thread_id, win32.WM_QUIT, 0, 0)

    def recent(self) -> list[tuple[float, int]]:
        """(time, hwnd) focus changes, newest first, one entry per window."""
        seen: set[int] = set()
        out = []
        for t, h in reversed(self._history):
            if h not in seen:
                seen.add(h)
                out.append((t, h))
        return out

    # -- thread ----------------------------------------------------------------

    def _run(self) -> None:
        self._thread_id = win32.GetCurrentThreadId()
        fg = win32.hwnd_int(win32.GetForegroundWindow())
        if fg:
            self._history.append((time.time(), fg))
        hook = win32.SetWinEventHook(
            win32.EVENT_SYSTEM_FOREGROUND, win32.EVENT_SYSTEM_FOREGROUND, None, self._event_proc, 0, 0, win32.WINEVENT_OUTOFCONTEXT
        )
        hwnd = self._make_clipboard_window()
        self.clip_seq = win32.GetClipboardSequenceNumber()
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while win32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                win32.TranslateMessage(ctypes.byref(msg))
                win32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if hook:
                win32.UnhookWinEvent(hook)
            if hwnd:
                ctypes.windll.user32.DestroyWindow(hwnd)

    def _make_clipboard_window(self) -> int:
        instance = win32.GetModuleHandleW(None)
        wc = win32.WNDCLASSW()
        wc.lpfnWndProc = self._wnd_proc
        wc.hInstance = instance
        wc.lpszClassName = f"ArgusClipboard{id(self)}"
        win32.RegisterClassW(ctypes.byref(wc))
        hwnd = win32.hwnd_int(
            win32.CreateWindowExW(0, wc.lpszClassName, "argus-clipboard", 0, 0, 0, 0, 0, win32.HWND_MESSAGE, None, instance, None)
        )
        if hwnd:
            win32.AddClipboardFormatListener(hwnd)
        return hwnd

    def _on_event(self, _hook, _event, hwnd, id_object, _id_child, _thread, _ms) -> None:
        if hwnd and id_object == 0:  # OBJID_WINDOW
            self._history.append((time.time(), win32.hwnd_int(hwnd)))

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == win32.WM_CLIPBOARDUPDATE:
            self.clip_seq = win32.GetClipboardSequenceNumber()
            self.clip_time = time.time()
            return 0
        return win32.DefWindowProcW(hwnd, msg, wparam, lparam)
