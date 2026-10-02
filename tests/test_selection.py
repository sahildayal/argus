import pytest

from argus.desktop import ArgusError, find_monitors, find_windows
from argus.geometry import Rect

from conftest import desk, mon, win

MONITORS = [
    mon(1, 0, 0, 2560, 1440, position="left", model="DELL U2723QE"),
    mon(2, 2560, 0, 2560, 1440, position="center", primary=True, model="LG 27GP850"),
    mon(3, 5120, 0, 2560, 1440, position="right", model="DELL S2721D"),
    mon(4, 2560, 1440, 1920, 1080, position="bottom", builtin=True, model="NV156FHM-N61"),
]


def make(**kw):
    layers = [
        win(10, "localhost:3000 - Dashboard - Google Chrome", app="Google Chrome", process="chrome.exe", rect=Rect(2560, 0, 2560, 1400), z=0, monitor=2),
        win(11, "app.tsx - web - Visual Studio Code", app="Visual Studio Code", process="Code.exe", rect=Rect(0, 0, 2560, 1400), z=1, monitor=1),
        win(12, "◑ Claude", app="Windows Terminal", process="WindowsTerminal.exe", rect=Rect(5120, 0, 2560, 1400), z=2, monitor=3, host=True),
        win(13, "Inbox - Gmail - Google Chrome", app="Google Chrome", process="chrome.exe", pid=10, rect=Rect(0, 0, 10, 10), z=3, minimized=True, monitor=None),
    ]
    return desk(MONITORS, layers, **kw)


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("2", [2]), (3, [3]), ("monitor 1", [1]), ("left", [1]), ("center", [2]), ("middle", [2]), ("right", [3]),
        ("bottom", [4]), ("laptop", [4]), ("primary", [2]), ("lg", [2]), ("all", [1, 2, 3, 4]),
        ("the left monitor", [1]), ("leftmost", [1]), ("DISPLAY3", [3]),
    ],
)
def test_monitor_selectors(spec, expected):
    assert [m.index for m in find_monitors(make(), spec)] == expected


def test_cursor_and_active_monitor():
    d = make(cursor=(6000, 500), fg=10)
    assert find_monitors(d, "cursor")[0].index == 3
    assert find_monitors(d, "active")[0].index == 2


def test_ambiguous_and_unknown_monitor_names_explain_the_options():
    with pytest.raises(ArgusError, match="several monitors"):
        find_monitors(make(), "dell")
    with pytest.raises(ArgusError, match="Monitors: Monitor 1"):
        find_monitors(make(), "samsung")
    with pytest.raises(ArgusError, match="no monitor 9"):
        find_monitors(make(), "9")


def test_window_by_title_app_process_and_handle():
    d = make()
    assert find_windows(d, "localhost:3000")[0].hwnd == 10
    assert find_windows(d, "vs code")[0].hwnd == 11
    assert find_windows(d, "visual studio")[0].hwnd == 11
    assert find_windows(d, "code")[0].hwnd == 11
    assert find_windows(d, "0xC")[0].hwnd == 12
    # Two Chrome windows: the visible one ranks above the minimized one.
    assert [w.hwnd for w in find_windows(d, "chrome")][:2] == [10, 13]


def test_window_fuzzy_match_tolerates_typos():
    assert find_windows(make(), "dashbord")[0].hwnd == 10


def test_no_window_match_lists_open_windows_but_not_the_host():
    with pytest.raises(ArgusError) as err:
        find_windows(make(), "photoshop")
    assert "Dashboard" in str(err.value) and "◑ Claude" not in str(err.value)


def test_active_window_selector_needs_a_focused_app():
    assert find_windows(make(fg=11), "active")[0].hwnd == 11
    with pytest.raises(ArgusError):
        find_windows(make(fg=0), "active")
