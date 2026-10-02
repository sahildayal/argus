from argus.geometry import Rect
from argus.looking import ago, choose

from conftest import desk, mon, win

MONS = [mon(1, 0, 0, 1920, 1080, position="left"), mon(2, 1920, 0, 1920, 1080, position="right")]
TERMINAL = win(1, "claude", app="Windows Terminal", process="WindowsTerminal.exe", rect=Rect(0, 0, 1920, 1040), z=0, host=True)
CHROME = win(2, "localhost:3000", app="Google Chrome", process="chrome.exe", rect=Rect(1920, 0, 1920, 1040), z=1, monitor=2)
CODE = win(3, "app.tsx", app="Visual Studio Code", process="Code.exe", rect=Rect(0, 0, 1920, 1040), z=2)
SLACK = win(4, "general", app="Slack", process="slack.exe", rect=Rect(1920, 0, 900, 700), z=3, monitor=2)


def test_active_non_chat_window_wins():
    d = desk(MONS, [CHROME, TERMINAL, CODE], fg=2)
    choice = choose(d, [])
    assert choice.window.hwnd == 2 and "active" in choice.reason
    assert choice.host is None


def test_chat_window_in_front_means_previous_window_from_history():
    d = desk(MONS, [TERMINAL, CODE, CHROME], fg=1, taken=1000.0)
    history = [(990.0, 1), (986.0, 2), (900.0, 3)]  # newest first: terminal, then chrome, then code
    choice = choose(d, history)
    assert choice.window.hwnd == 2
    assert "before switching to Windows Terminal" in choice.reason and "14s" in choice.reason
    assert choice.host.hwnd == 1


def test_without_history_falls_back_to_front_most_other_window():
    d = desk(MONS, [TERMINAL, CODE, CHROME], fg=1)
    choice = choose(d, [])
    assert choice.window.hwnd == 3 and "front-most window behind" in choice.reason


def test_window_under_mouse_is_offered_as_alternative():
    d = desk(MONS, [TERMINAL, CODE, CHROME, SLACK], fg=1, under=4, cursor=(2000, 100))
    choice = choose(d, [(999.0, 1), (990.0, 3)])
    assert choice.window.hwnd == 3
    assert choice.alternatives[0] == ("under your mouse", SLACK)


def test_minimized_and_closed_windows_are_skipped():
    gone = 99  # in history but no longer open
    minimized = win(5, "notes", app="Notepad", process="notepad.exe", z=4, minimized=True, monitor=None)
    d = desk(MONS, [TERMINAL, minimized, CHROME], fg=1)
    choice = choose(d, [(999.0, 1), (998.0, gone), (997.0, 5), (996.0, 2)])
    assert choice.window.hwnd == 2


def test_nothing_else_open():
    d = desk(MONS, [TERMINAL], fg=1)
    assert choose(d, []).window is None


def test_ago_formatting():
    assert ago(4.6) == "4s" and ago(125) == "2 min" and ago(3720) == "1 h 2 min"
