import io
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from argus import gate
from argus.config import Settings, set_profile
from argus.geometry import Rect

from conftest import desk, mon, win


def _wait(root, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        root.update()
        if condition():
            return
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _widgets(parent):
    for child in parent.winfo_children():
        yield child
        yield from _widgets(child)


def _button(window, label):
    return next(widget for widget in _widgets(window)
                if widget.winfo_class() == "TButton" and widget.cget("text") == label)


@pytest.fixture(scope="module")
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture
def approval_ui(tk_root, monkeypatch):
    from argus import gate_ui

    set_profile("work")
    monitor = mon(1, 0, 0, 1000, 800, primary=True)
    window = win(1, "Allowed title", process="Code.exe")
    hidden = win(2, "Hidden title", process="unknown.exe")
    monkeypatch.setattr(gate_ui, "snapshot", lambda _host: desk([monitor], [window, hidden]))
    s = Settings.from_toml('[profile]\nactive = "work"\n[work]\nallow_apps = ["Code.exe"]')
    monkeypatch.setattr(gate_ui, "settings", lambda: s)
    monkeypatch.setattr(gate_ui, "_work_area", lambda _top: Rect(0, 0, 1000, 800))
    monkeypatch.setattr(gate_ui.win32, "SetPropW", lambda *_: True)
    monkeypatch.setattr(gate_ui.win32, "SetWindowDisplayAffinity", lambda *_: True)
    ui = gate_ui.Approvals(tk_root)
    try:
        yield ui, gate_ui
    finally:
        ui.close()
        for child in tk_root.winfo_children():
            child.destroy()
        tk_root.update_idletasks()


def _open(ui, gate_ui, *, seconds=5, allow_trust=True, images=(), query=""):
    current = gate.policy()
    preview = gate.Preview(
        "test", "screenshot", images, ("Screen caption",),
        frozenset({(1, "code.exe", 1), (2, "unknown.exe", 2)}),
        current.stamp, time.monotonic() + seconds, allow_trust, False, query,
    )
    pending = gate_ui._Pending(preview)
    ui.queue.put(pending)
    ui.poll()
    assert ui.window is not None
    return pending


def test_send_and_trust_arm_only_after_focus_settles(approval_ui, monkeypatch):
    """The arming logic, driven by synthetic focus events, so it doesn't depend
    on Windows letting this process take the foreground."""
    ui, gate_ui = approval_ui
    pending = _open(ui, gate_ui)
    window, root = ui.window, ui.root
    deny, send, trust = (_button(window, label) for label in ("Deny", "Send", "Send & trust"))
    focus = {"widget": deny}
    monkeypatch.setattr(window, "focus_displayof", lambda: focus["widget"])

    def focus_event(kind):
        window.event_generate(kind, detail="NotifyNonlinear")
        root.update()

    focus_event("<FocusOut>")
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    gained = time.monotonic()
    focus_event("<FocusIn>")
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    _wait(root, lambda: send.instate(["!disabled"]))
    assert time.monotonic() - gained >= gate_ui._ARM_MS / 1000 - 0.05
    assert trust.instate(["!disabled"])

    # Losing focus disarms at once; regaining it starts the delay again.
    focus_event("<FocusOut>")
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    send.invoke()
    assert not pending.done.is_set()

    # Focus that moved elsewhere by the time the delay ends doesn't arm.
    focus_event("<FocusIn>")
    focus["widget"] = None
    deadline = time.monotonic() + gate_ui._ARM_MS / 1000 + 0.3
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert send.instate(["disabled"]) and trust.instate(["disabled"])

    focus["widget"] = deny
    focus_event("<FocusIn>")
    _wait(root, lambda: trust.instate(["!disabled"]))
    trust.invoke()
    assert pending.done.is_set() and pending.reason == "user"
    assert ui.trust.covers(pending.preview, gate.policy())


# Needs the OS to give this process real keyboard focus (focus_force), which
# Windows refuses to background processes, so it only runs with -m live.
@pytest.mark.live
def test_send_and_trust_rearm_after_each_focus_gain(approval_ui):
    ui, gate_ui = approval_ui
    pending = _open(ui, gate_ui)
    window = ui.window
    root = ui.root
    deny, send, trust = (_button(window, label) for label in ("Deny", "Send", "Send & trust"))
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    assert int(send.cget("takefocus")) == int(trust.cget("takefocus")) == 0
    shown_at = time.monotonic()
    root.update()
    assert window.focus_lastfor() is deny
    assert deny.tk_focusNext() is deny
    assert deny.bind("<Return>") and send.bind("<space>") and trust.bind("<space>")
    send.invoke()
    assert not pending.done.is_set()

    deny.focus_force()
    root.update()
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    _wait(root, lambda: send.instate(["!disabled"]))
    assert time.monotonic() - shown_at >= gate_ui._ARM_MS / 1000 - 0.05
    assert trust.instate(["!disabled"])

    other = gate_ui.tk.Toplevel(root)
    other.update()
    other.focus_force()
    root.update()
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    focused_at = time.monotonic()
    deny.focus_force()
    root.update()
    assert send.instate(["disabled"]) and trust.instate(["disabled"])
    _wait(root, lambda: trust.instate(["!disabled"]))
    assert time.monotonic() - focused_at >= gate_ui._ARM_MS / 1000 - 0.05
    assert send.instate(["!disabled"])
    trust.invoke()
    assert pending.done.is_set() and pending.reason == "user"
    assert ui.trust.covers(pending.preview, gate.policy())


def test_timeout_closes_prompt_and_ineligible_trust_is_hidden(approval_ui):
    ui, gate_ui = approval_ui
    pending = _open(ui, gate_ui, seconds=0.6, allow_trust=False)
    window = ui.window
    assert all(widget.cget("text") != "Send & trust" for widget in _widgets(window) if widget.winfo_class() == "TButton")
    _wait(ui.root, pending.done.is_set)
    assert pending.reason == "timeout"
    assert ui.window is None and not window.winfo_exists()
    assert not ui.trust.covers(pending.preview, gate.policy())


def test_enter_does_nothing_and_escape_denies(approval_ui):
    ui, gate_ui = approval_ui
    pending = _open(ui, gate_ui, allow_trust=False)
    deny = _button(ui.window, "Deny")
    ui.root.update()
    deny.focus_force()
    _wait(ui.root, lambda: ui.root.focus_get() is deny)
    deny.event_generate("<Return>")
    deny.event_generate("<KP_Enter>")
    assert not pending.done.is_set()
    deny.event_generate("<Escape>")
    assert pending.done.is_set() and pending.reason == "denied"
    assert ui.window is None


def test_preview_shows_grid_text_summary_and_trust_scope(approval_ui):
    ui, gate_ui = approval_ui
    data = io.BytesIO()
    Image.new("RGB", (1000, 500), "navy").save(data, format="PNG")
    pending = _open(ui, gate_ui, images=(data.getvalue(),) * 3, query="Work roster")
    window = ui.window
    ui.root.update()
    widgets = list(_widgets(window))
    summary = f"3 images + {len('Requested by the AI: Work roster') + len('Screen caption')} characters of text, tool: screenshot"
    assert summary in [w.cget("text") for w in widgets if w.winfo_class() == "TLabel" and w.winfo_ismapped()]
    assert any(w.winfo_class() == "TLabelframe" and w.cget("text") == gate.QUERY_LABEL for w in widgets)
    text = [w.get("1.0", "end-1c") for w in widgets if w.winfo_class() == "Text" and w.winfo_ismapped()]
    assert "Screen caption" in text and "Trust 2 windows for 10 min:\nCode.exe — Allowed title\nunknown.exe" in text
    assert window.winfo_width() == 700 and window.winfo_height() == 560

    canvas = next(w for w in widgets if w.winfo_class() == "Canvas")
    assert canvas.winfo_ismapped()
    assert sum(canvas.type(item) == "image" for item in canvas.find_all()) == 3
    x, y = map(int, canvas.coords(canvas.find_withtag("thumbnail-0")[0]))
    canvas.event_generate("<Motion>", x=x, y=y)
    canvas.event_generate("<Button-1>", x=x, y=y)
    (image,) = canvas.find_all()
    x0, y0, x1, y1 = canvas.bbox(image)
    assert x0 >= 0 and y0 >= 0 and x1 <= canvas.winfo_width() and y1 <= canvas.winfo_height()
    assert abs((x1 - x0) - 2 * (y1 - y0)) <= 2
    _button(window, "100%").invoke()
    x0, y0, x1, y1 = canvas.bbox(canvas.find_all()[0])
    assert (x1 - x0, y1 - y0) == (1000, 500)
    _button(window, "All images").invoke()
    assert sum(canvas.type(item) == "image" for item in canvas.find_all()) == 3
    assert not pending.done.is_set()


@pytest.mark.live
def test_tray_serializes_prompts_and_revokes_trust():
    import tkinter

    from argus.gate_ui import Approvals

    set_profile("work")
    root = tkinter.Tk()
    root.withdraw()
    approvals = Approvals(root)
    current = gate.policy()
    deadline = time.monotonic() + 10

    def preview(request_id, hwnd):
        return gate.Preview(request_id, "screenshot", (), ("Synthetic caption",),
                            frozenset({(hwnd, "test.exe", 1)}), current.stamp, deadline, True, False)

    def wait_until_queued():
        while approvals.queue.empty():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            a = workers.submit(approvals.answer, preview("first", 1))
            wait_until_queued()
            approvals.poll()
            original = approvals.window
            b = workers.submit(approvals.answer, preview("second", 2))
            wait_until_queued()
            approvals.poll()
            assert approvals.window is original
            assert approvals.active.preview.request_id == "first"
            _button(original, "Deny").focus_force()
            _wait(root, lambda: _button(original, "Send & trust").instate(["!disabled"]))
            _button(original, "Send & trust").invoke()
            assert a.result(1) == "user"
            assert approvals.trust.covers(preview("same-window", 1), current)
            approvals.trust.revoke()
            assert not approvals.trust.covers(preview("same-window", 1), current)
            approvals.poll()
            assert approvals.window is None

            def show_next():
                approvals.poll()
                return approvals.window is not None

            _wait(root, show_next)
            assert approvals.active.preview.request_id == "second"
            _button(approvals.window, "Deny").invoke()
            assert b.result(1) == "denied"
        finally:
            approvals.close()
            root.destroy()
