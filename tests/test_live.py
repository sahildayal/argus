"""Live tests against throwaway windows (pytest -m live). They only ever capture
their own topmost test windows (or regions inside them), never the user's."""

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from argus import ocr
from argus.capture import capture_region, capture_window
from argus.config import settings
from argus.desktop import NO_HOST, find_windows, set_host, snapshot
from argus.geometry import Rect

pytestmark = pytest.mark.live
WINDOW_SCRIPT = Path(__file__).with_name("live_window.py")


def _open(title, geometry="900x560+140+140", topmost=True, change_after=0.0):
    args = [sys.executable, str(WINDOW_SCRIPT), "--title", title, "--geometry", geometry]
    if topmost:
        args.append("--topmost")
    if change_after:
        args += ["--change-after", str(change_after)]
    proc = subprocess.Popen(args)
    deadline = time.time() + 15
    while time.time() < deadline:
        desk = snapshot(NO_HOST)
        found = [w for w in desk.windows if w.title == title]
        if found:
            time.sleep(0.6)  # let it paint
            return proc, found[0]
        time.sleep(0.2)
    proc.kill()
    raise AssertionError(f"test window {title!r} never appeared")


@pytest.fixture
def window(request):
    set_host(NO_HOST)
    procs = []

    def make(**kw):
        title = kw.pop("title", None) or f"Argus Test {uuid.uuid4().hex[:6]}"
        proc, win = _open(title, **kw)
        procs.append(proc)
        return win

    yield make
    for p in procs:
        p.kill()


def _has_colour(img, rgb, tolerance=10):
    small = img.convert("RGB")
    pixels = small.get_flattened_data()
    return any(all(abs(a - b) <= tolerance for a, b in zip(px, rgb)) for px in pixels)


def test_visible_window_is_cropped_from_the_screen(window):
    win = window()
    desk = snapshot(NO_HOST)
    shot = capture_window(desk, desk.window(win.hwnd), settings())
    assert shot.method == "screen"
    assert abs(shot.image.width - win.rect.w) <= 2 and abs(shot.image.height - win.rect.h) <= 2
    for rgb in ((255, 0, 0), (0, 192, 0), (0, 0, 255)):
        assert _has_colour(shot.image, rgb)


def test_covered_window_renders_itself(window):
    below = window(geometry="900x560+160+160", topmost=False)
    window(geometry="600x400+300+300", topmost=True)  # covers part of it
    desk = snapshot(NO_HOST)
    target = desk.window(below.hwnd)
    assert desk.visible_fraction(target) < 0.99
    shot = capture_window(desk, target, settings())
    assert shot.method == "printwindow", shot.notes
    for rgb in ((255, 0, 0), (0, 192, 0), (0, 0, 255)):
        assert _has_colour(shot.image, rgb)


def test_ocr_reads_the_test_window(window):
    win = window()
    desk = snapshot(NO_HOST)
    shot = capture_window(desk, desk.window(win.hwnd), settings())
    lines = asyncio.run(ocr.read(shot.image))
    text = "\n".join(ln.text for ln in lines)
    assert "Cannot find name" in text and "Submit" in text
    assert ocr.find(lines, "submit")


def test_privacy_list_blacks_out_matching_windows(window, argus_home):
    settings()  # writes the default config into the test home
    cfg = argus_home / "config.toml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("redact_titles = [", "redact_titles = [\n  '\\bargus secret\\b',"), encoding="utf-8")
    import os

    os.utime(cfg, (time.time() + 5, time.time() + 5))
    win = window(title="Argus Secret Window")
    desk = snapshot(NO_HOST)
    inner = Rect(win.rect.x + 40, win.rect.y + 60, 300, 200)
    shot = capture_region(desk, inner, settings())
    assert shot.hidden and shot.image.getpixel((5, 5)) == (24, 24, 27)
    with pytest.raises(Exception, match="privacy list"):
        capture_window(desk, desk.window(win.hwnd), settings())


def test_find_windows_by_partial_title(window):
    win = window(title="Argus Test Findme 42")
    assert find_windows(snapshot(NO_HOST), "findme 42")[0].hwnd == win.hwnd


def test_wait_for_text_and_compare_with_service(window):
    from argus.service import Session

    win = window(change_after=2.5)
    session = Session(host=NO_HOST, track=False)
    session.save_baseline("live-before", window=win.handle)
    result = asyncio.run(session.wait_for("text", "Compiled successfully", window=win.handle, timeout=20))
    assert result.parts[0].startswith("Done"), result.parts[0]
    diff = session.compare(baseline="live-before")
    assert "changed" in diff.parts[0] and diff.captures


def _pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


ACCENT_RGB = (0, 209, 255)


@pytest.fixture(scope="module")
def tray_app():
    """One TrayApp per test run: Tcl can't re-create Tk in a process once destroyed."""
    from argus.tray import TrayApp

    app = TrayApp()
    yield app
    app.root.destroy()


def test_tray_flash_appears_then_vanishes(window, tray_app):
    win = window()
    tray_app.flash(win.rect, "Marked for Argus: test", ms=1200)
    _pump(tray_app.root, 0.4)
    border = Rect(win.rect.x, win.rect.y, 40, win.rect.h)  # left edge, where the border is drawn
    assert _has_colour(capture_region(snapshot(NO_HOST), border, settings()).image, ACCENT_RGB, tolerance=30)
    _pump(tray_app.root, 1.2)
    assert not _has_colour(capture_region(snapshot(NO_HOST), border, settings()).image, ACCENT_RGB, tolerance=30)


def test_tray_mark_saves_the_window_under_the_mouse(window, tray_app, monkeypatch):
    from dataclasses import replace

    from PIL import Image

    import argus.tray as tray
    from argus.capture import Shot
    from argus.marks import recent_marks

    win = window(title="Argus Test Mark Target")
    real_snapshot = tray.snapshot
    monkeypatch.setattr(tray, "snapshot", lambda host=None: replace(real_snapshot(NO_HOST), under_cursor=win.hwnd))
    # Don't capture the user's real monitor in a test.
    monkeypatch.setattr(tray, "capture_monitor", lambda desk, mon, s: Shot(Image.new("RGB", (64, 36)), "monitor", mon.title, (0, 0), {}))
    tray_app.mark()
    _pump(tray_app.root, 0.2)
    mark = recent_marks(1)[0]
    assert "Argus Test Mark Target" in mark.info["window"]
    assert _has_colour(mark.shot("window").image, (255, 0, 0))


def test_hotkey_thread_registers_and_dispatches():
    import queue as q

    from argus import win32
    from argus.config import parse_hotkey
    from argus.tray import Hotkeys

    events = q.Queue()
    keys = Hotkeys(events, {7: ("mark", "ctrl+alt+shift+f24")})
    keys.start()
    assert events.get(timeout=5) == ("hotkeys", [])
    mods, vk = parse_hotkey("ctrl+alt+shift+f24")
    assert not win32.RegisterHotKey(None, 99, mods, vk)  # taken by the thread above
    win32.PostThreadMessageW(keys.thread_id, win32.WM_HOTKEY, 7, 0)  # as if the keys were pressed
    assert events.get(timeout=5) == ("mark", None)
    keys.stop()
    keys.join(5)


def test_stdio_server_round_trip(window):
    """The real server process, over stdio, as an MCP client would run it."""
    import base64
    import io
    import os

    from mcp import Client, StdioServerParameters
    from PIL import Image

    win = window()
    params = StdioServerParameters(command=sys.executable, args=["-m", "argus.server"], env={**os.environ})

    async def go():
        async with Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool("screenshot", {"window": win.handle})
            return tools, result

    tools, result = asyncio.run(go())
    assert len(tools.tools) == 11
    assert not result.is_error, result.content[0].text
    image = next(c for c in result.content if c.type == "image")
    assert Image.open(io.BytesIO(base64.b64decode(image.data))).size[0] >= 800
