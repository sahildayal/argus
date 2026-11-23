"""Tray-thread approval UI and queue. Pipe workers never call Tk."""

from __future__ import annotations

import io
import logging
import math
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

from PIL import Image, ImageTk

from . import gate, privacy, win32
from .config import settings
from .desktop import NO_HOST, snapshot
from .geometry import Rect
from .text import display_arg

log = logging.getLogger("argus.tray")
_ARM_MS = 750
_PROMPT_GAP = 0.3


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
        self._arm_job: str | None = None
        self._deadline_job: str | None = None
        self._armed = False
        self._next_prompt = 0.0
        self._offset = 0

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
            if time.monotonic() < self._next_prompt:
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
        self._disarm()
        if self._deadline_job is not None:
            self.root.after_cancel(self._deadline_job)
            self._deadline_job = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
            self._next_prompt = time.monotonic() + _PROMPT_GAP
            self._offset = 20 - self._offset
        if self.active is not None:
            self.active.finish(reason)
            self.active = None

    def close(self) -> None:
        self._finish("tray_unavailable")
        while not self.queue.empty():
            self.queue.get_nowait().finish("tray_unavailable")

    def _send(self, trust: bool) -> None:
        if self.active is None or not self._armed:
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
            self._finish("denied" if trust and not self.trust.grant(preview, current) else "user")
        except (OSError, ValueError, gate.GateStopped):
            self._finish("tray_unavailable")

    def _show(self, preview: gate.Preview, minutes: float) -> None:
        top = self.window = tk.Toplevel(self.root)
        top.withdraw()
        top.title("Argus approval")
        top.attributes("-topmost", True)
        work = _work_area(top)
        width, height = int(work.w * 0.7), int(work.h * 0.7)
        top.geometry(f"{width}x{height}")
        top.minsize(min(width, 480), min(height, 330))
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
        requested = display_arg(preview.query)
        characters = sum(map(len, preview.text))
        if requested:
            characters += len(gate.QUERY_LABEL) + 2 + len(requested)
        count = len(preview.images)
        ttk.Label(top, text=f"{count} {'image' if count == 1 else 'images'} + {characters} characters of text, tool: {preview.tool}").pack(
            anchor="w", padx=12, pady=(0, 6)
        )
        if preview.watching:
            ttk.Label(
                top, text="The window set changed. Approve this earlier frame for use in the watch. The final result is reviewed separately.",
                wraplength=max(1, width - 24),
            ).pack(anchor="w", padx=12, pady=(0, 6))

        buttons = ttk.Frame(top, padding=12)
        buttons.pack(side="bottom", fill="x")
        deny = ttk.Button(buttons, text="Deny", command=lambda: self._finish("denied"))
        deny.pack(side="right", padx=(6, 0))
        send = ttk.Button(buttons, text="Send", takefocus=False, state="disabled", command=lambda: self._send(False))
        send.pack(side="right", padx=(6, 0))
        approvals = [send]
        if preview.allow_trust and preview.windows:
            trust = ttk.Button(buttons, text="Send & trust", takefocus=False, state="disabled", command=lambda: self._send(True))
            trust.pack(side="right", padx=(6, 0))
            approvals.append(trust)
            labels = _trust_labels(preview.windows)
            scope = ttk.Frame(buttons)
            scope.pack(side="left", fill="both", expand=True)
            _text_panel(scope, f"Trust for {minutes:g} min: {', '.join(labels)}", height=2, width=1)
        for button in [deny, *approvals]:
            button.bind("<Return>", lambda _event: "break")
            button.bind("<KP_Enter>", lambda _event: "break")
        for button in approvals:
            button.bind("<space>", lambda _event: "break")

        if requested:
            query = ttk.LabelFrame(top, text=gate.QUERY_LABEL, padding=(8, 4))
            query.pack(fill="x", padx=12, pady=(0, 6))
            ttk.Label(query, text=requested, wraplength=max(1, width - 48)).pack(anchor="w")

        panes = ttk.Panedwindow(top, orient="horizontal", takefocus=False)
        panes.pack(fill="both", expand=True, padx=12)
        if preview.images:
            images = ttk.Frame(panes, width=int(width * 0.57))
            panes.add(images, weight=3)
            _image_panel(images, preview.images)
        if preview.text:
            body = ttk.Frame(panes)
            panes.add(body, weight=2)
            _text_panel(body, "\n\n".join(preview.text), width=max(1, width // 26))

        def enable() -> None:
            self._arm_job = None
            focused = top.focus_displayof()
            if focused is not None and focused.winfo_toplevel() is top:
                self._armed = True
                for button in approvals:
                    button.state(["!disabled"])

        def on_focus(event: tk.Event) -> None:
            if event.widget is not top:
                return
            self._disarm()
            for button in approvals:
                button.state(["disabled"])
            if event.type == tk.EventType.FocusIn:
                self._arm_job = self.root.after(_ARM_MS, enable)

        top.bind("<FocusIn>", on_focus)
        top.bind("<FocusOut>", on_focus)
        deny.bind("<Map>", lambda _event: deny.focus_set())
        top.deiconify()
        x = work.x + (work.w - width) // 2 + self._offset
        y = work.y + (work.h - height) // 2 + self._offset
        win32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x15)  # keep size, Z order and activation
        top.lift()
        delay = max(0, math.ceil((preview.deadline - time.monotonic()) * 1000))
        self._deadline_job = self.root.after(delay, lambda: self._finish("timeout"))

    def _disarm(self) -> None:
        self._armed = False
        if self._arm_job is not None:
            self.root.after_cancel(self._arm_job)
            self._arm_job = None


def _work_area(top: tk.Toplevel) -> Rect:
    cursor = win32.cursor_pos()
    monitors = win32.monitors_raw()
    for _, _, bounds, work, _, _ in monitors:
        if bounds.contains(*cursor):
            return work
    return next((work for _, _, _, work, primary, _ in monitors if primary),
                Rect(0, 0, top.winfo_screenwidth(), top.winfo_screenheight()))


def _trust_labels(windows: privacy.WindowSet) -> list[str]:
    s = settings()
    names = {
        privacy.window_identity(w): (w.process, w.title)
        for w in snapshot(NO_HOST).layers if privacy.shown(w, s)
    }
    labels = []
    for hwnd, exe, pid in sorted(windows, key=lambda window: (window[1], window[0])):
        name, title = names.get((hwnd, exe, pid), (exe, ""))
        label = display_arg(name) or "Unknown app"
        title = display_arg(title)
        labels.append(f"{label} — {title}" if title else label)
    return labels


def _text_panel(parent: ttk.Frame, text: str, **size: int) -> None:
    widget = tk.Text(parent, wrap="word", takefocus=False, font=("Segoe UI", 10), padx=6, pady=4, **size)
    widget.insert("1.0", text)
    widget.configure(state="disabled")
    _scrollable(parent, widget, horizontal=False)


def _image_panel(parent: ttk.Frame, encoded: tuple[bytes, ...]) -> None:
    images = []
    for data in encoded:
        with Image.open(io.BytesIO(data)) as image:
            images.append(image.copy())

    controls = ttk.Frame(parent)
    controls.pack(fill="x")
    gallery = ttk.Button(controls, text="All images", takefocus=False)
    zoom = ttk.Button(controls, text="100%", takefocus=False)
    for button in (gallery, zoom):
        button.bind("<space>", lambda _event: "break")
    selected = 0 if len(images) == 1 else None
    actual_size = False
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    canvas = tk.Canvas(frame, highlightthickness=0, takefocus=False)
    _scrollable(frame, canvas)
    photos = []

    def draw(_event=None) -> None:
        canvas.delete("all")
        photos.clear()
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if selected is not None:
            image = images[selected]
            if not actual_size:
                image = _fit(image, width, height)
            photo = ImageTk.PhotoImage(image, master=canvas)
            photos.append(photo)
            canvas.create_image(max(0, (width - photo.width()) // 2), max(0, (height - photo.height()) // 2),
                                image=photo, anchor="nw")
            canvas.configure(scrollregion=(0, 0, max(width, photo.width()), max(height, photo.height())))
            return

        cols = math.ceil(math.sqrt(len(images)))
        rows = math.ceil(len(images) / cols)
        cell_w, cell_h = width / cols, height / rows
        for i, image in enumerate(images):
            x, y = (i % cols + 0.5) * cell_w, (i // cols) * cell_h
            photo = ImageTk.PhotoImage(_fit(image, int(cell_w) - 8, int(cell_h) - 25), master=canvas)
            photos.append(photo)
            tag = f"thumbnail-{i}"
            canvas.create_image(x, y + (cell_h - 20) / 2, image=photo, tags=tag)
            canvas.create_text(x, y + cell_h - 10, text=f"Image {i + 1}", tags=tag)
        canvas.configure(scrollregion=(0, 0, width, height))

    def select(index: int | None) -> None:
        nonlocal selected, actual_size
        selected, actual_size = index, False
        gallery.state(["disabled" if index is None else "!disabled"])
        zoom.state(["disabled" if index is None else "!disabled"])
        zoom.configure(text="100%")
        redraw_from_origin()

    def toggle_zoom() -> None:
        nonlocal actual_size
        actual_size = not actual_size
        zoom.configure(text="Fit" if actual_size else "100%")
        redraw_from_origin()

    def redraw_from_origin() -> None:
        draw()
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)

    if len(images) > 1:
        gallery.configure(command=lambda: select(None))
        gallery.pack(side="left")
        for i in range(len(images)):
            canvas.tag_bind(f"thumbnail-{i}", "<Button-1>", lambda _event, index=i: select(index))
    zoom.configure(command=toggle_zoom)
    zoom.pack(side="left")
    canvas.bind("<Configure>", draw)
    select(selected)


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = min(1, max(1, width) / image.width, max(1, height) / image.height)
    if scale == 1:
        return image
    return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)


def _scrollable(parent: ttk.Frame, widget, *, horizontal: bool = True) -> None:
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    widget.grid(row=0, column=0, sticky="nsew")
    y = ttk.Scrollbar(parent, orient="vertical", takefocus=False, command=widget.yview)
    y.grid(row=0, column=1, sticky="ns")
    widget.configure(yscrollcommand=y.set)
    if horizontal:
        x = ttk.Scrollbar(parent, orient="horizontal", takefocus=False, command=widget.xview)
        x.grid(row=1, column=0, sticky="ew")
        widget.configure(xscrollcommand=x.set)
    widget.bind("<MouseWheel>", lambda e: widget.yview_scroll(-e.delta // 120, "units"))
