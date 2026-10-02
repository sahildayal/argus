"""Shared fixtures. Unit tests never touch the real screen: they build fake
desktops from these factories. Live tests (pytest -m live) open their own
throwaway windows and only capture those."""

from __future__ import annotations

import os
import tempfile

# Before anything imports argus.server (which reads settings at import time),
# point Argus at a throwaway home so tests never touch the real ~/.argus.
os.environ["ARGUS_HOME"] = tempfile.mkdtemp(prefix="argus-test-")

import pytest  # noqa: E402

from argus.desktop import NO_HOST, Desktop, Host, Monitor, Window  # noqa: E402
from argus.geometry import Rect  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "live: opens test windows and captures them (needs a real desktop)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") and "live" in config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="live test: run with  pytest -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def argus_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.argus."""
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path / "argus-home"))
    import argus.config as config

    monkeypatch.setattr(config, "_cache", None)
    return tmp_path / "argus-home"


def mon(index, x, y, w, h, *, position="", primary=False, builtin=False, model="DELL U2723QE", dpi=96) -> Monitor:
    return Monitor(index, f"\\\\.\\DISPLAY{index}", model, builtin, primary, Rect(x, y, w, h), Rect(x, y, w, h - 40), dpi, position)


def win(hwnd, title, *, app="App", process="app.exe", pid=None, rect=Rect(0, 0, 800, 600), z=0, minimized=False,
        is_app=True, see_through=False, owner=0, monitor=1, host=False, cls="AppWindow", topmost=False,
        maximized=False) -> Window:
    return Window(hwnd=hwnd, title=title, cls=cls, pid=pid or hwnd, process=process, app=app, rect=rect, z=z,
                  minimized=minimized, maximized=maximized, topmost=topmost, see_through=see_through, is_app=is_app,
                  owner=owner, monitor=monitor, host=host)


def desk(monitors, layers, *, cursor=(10, 10), fg=0, under=0, host: Host = NO_HOST, taken=1000.0) -> Desktop:
    return Desktop(tuple(monitors), tuple(layers), cursor, fg, under, host, taken)
