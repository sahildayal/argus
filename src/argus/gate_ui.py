"""Tray-thread approval UI and queue. Pipe workers never call Tk."""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

from PIL import Image, ImageTk

from . import gate, win32

log = logging.getLogger("argus.tray")


@dataclass
class _Pending:
    preview: gate.Preview
    done: threading.Event = field(default_factory=threading.Event)
    reason: str = "denied"

    def finish(self, reason: str) -> None:
        self.reason = reason
        self.done.set()


class Approvals:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.trust = gate.Trust()
        self.queue: queue.Queue[_Pending] = queue.Queue()
        self.active: _Pending | None = None
        self.window: tk.Toplevel | None = None

    def answer(self, preview: gate.Preview) -> str:
        pending = _Pending(preview)
        self.queue.put(pending)
        if not pending.done.wait(max(0, preview.deadline - time.monotonic())):
            return "timeout"
        return pending.reason

    def poll(self) -> None:
        try:
            current = gate.policy()
            self.trust.refresh(current)
            if self.active:
                if self.active.preview.deadline <= time.monotonic():
                    self._finish("timeout")
                elif not self.trust.valid(self.active.preview, current):
                    self._finish("denied")
                else:
                    return
            while not self.queue.empty():
                self.active = self.queue.get_nowait()
                preview = self.active.preview
                if preview.deadline <= time.monotonic():
                    self._finish("timeout")
                elif not self.trust.valid(preview, current):
                    self._finish("denied")
                elif self.trust.covers(preview, current):
                    self._finish("trusted")
                else:
                    self._show(preview, current.trust_minutes)
                    return
        except Exception:
            log.warning("Could not display or check an Argus approval")
            self._finish("tray_unavailable")

    def _finish(self, reason: str) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None
        if self.active is not None:
            self.active.finish(reason)
            self.active = None

    def close(self) -> None:
        self._finish("tray_unavailable")
        while not self.queue.empty():
            self.queue.get_nowait().finish("tray_unavailable")

    def _send(self, trust: bool) -> None:
        if self.active is None:
            return
        try:
            preview = self.active.preview
            current = gate.policy()
            if preview.deadline <= time.monotonic():
                self._finish("timeout")
                return
            if not self.trust.valid(preview, current):
                self._finish("denied")
                return
            if trust:
                self.trust.grant(preview, current)
            self._finish("user")
        except (OSError, ValueError, gate.GateStopped):
            self._finish("tray_unavailable")

    def _show(self, preview: gate.Preview, minutes: float) -> None:
        top = self.window = tk.Toplevel(self.root)
        top.withdraw()
        top.title("Argus approval")
        top.attributes("-topmost", True)
        top.geometry("740x500")
        top.minsize(480, 310)
        top.protocol("WM_DELETE_WINDOW", lambda: self._finish("denied"))
        top.bind("<Escape>", lambda _event: self._finish("denied"))
        top.bind("<Return>", lambda _event: "break")
        top.bind("<KP_Enter>", lambda _event: "break")
        top.update_idletasks()
        hwnd = win32.GetAncestor(top.winfo_id(), win32.GA_ROOT)
        # Prevent other Argus clients from capturing another pending preview,
        # through desktop capture or through direct window capture.
        if not (hwnd and win32.SetPropW(hwnd, win32.APPROVAL_PREVIEW, 1)
                and win32.SetWindowDisplayAffinity(hwnd, 0x11)):  # WDA_EXCLUDEFROMCAPTURE
            raise OSError("Could not protect the approval preview")

        ttk.Label(top, text=f"Share with the AI via {preview.tool}?", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        if preview.watching:
            ttk.Label(
                top, text="The window set changed. Approve this earlier frame for use in the watch. The final result is reviewed separately.",
                wraplength=690,
            ).pack(anchor="w", padx=12, pady=(0, 6))

        buttons = ttk.Frame(top, padding=12)
        buttons.pack(side="bottom", fill="x")
        deny = ttk.Button(buttons, text="Deny", command=lambda: self._finish("denied"))
        deny.pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Send", command=lambda: self._send(False)).pack(side="right", padx=(6, 0))
        if preview.windows and preview.tool != "list_screens":
            ttk.Button(buttons, text=f"Send & trust these windows for {minutes:g} min", command=lambda: self._send(True)).pack(
                side="right"
            )
        for button in buttons.winfo_children():
            button.bind("<Return>", lambda _event: "break")
            button.bind("<KP_Enter>", lambda _event: "break")

        notebook = ttk.Notebook(top)
        notebook.pack(fill="both", expand=True, padx=12)
        for index, encoded in enumerate(preview.images, 1):
            tab = ttk.Frame(notebook)
            notebook.add(tab, text=f"Image {index}")
            with Image.open(io.BytesIO(encoded)) as image:
                photo = ImageTk.PhotoImage(image, master=tab)
            canvas = tk.Canvas(tab, highlightthickness=0, scrollregion=(0, 0, photo.width(), photo.height()))
            _scrollable(tab, canvas)
            canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas.photo = photo
        if preview.text:
            tab = ttk.Frame(notebook)
            notebook.add(tab, text="Text")
            widget = tk.Text(tab, wrap="word", font=("Segoe UI", 10), padx=8, pady=8)
            widget.insert("1.0", "\n\n".join(preview.text))
            widget.configure(state="disabled")
            _scrollable(tab, widget, horizontal=False)
        top.deiconify()
        top.lift()
        deny.focus_set()


def _scrollable(parent: ttk.Frame, widget, *, horizontal: bool = True) -> None:
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    widget.grid(row=0, column=0, sticky="nsew")
    y = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
    y.grid(row=0, column=1, sticky="ns")
    widget.configure(yscrollcommand=y.set)
    if horizontal:
        x = ttk.Scrollbar(parent, orient="horizontal", command=widget.xview)
        x.grid(row=1, column=0, sticky="ew")
        widget.configure(xscrollcommand=x.set)
    widget.bind("<MouseWheel>", lambda e: widget.yview_scroll(-e.delta // 120, "units"))
