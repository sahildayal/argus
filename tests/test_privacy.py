import time

from PIL import Image

from argus import privacy
from argus.config import Settings, DEFAULT_CONFIG
from argus.desktop import Paused
from argus.geometry import Rect

from conftest import desk, mon, win

S = Settings.from_toml(DEFAULT_CONFIG)


def test_sensitive_apps_titles_and_browser_tabs():
    assert privacy.sensitive_reason(win(1, "WhatsApp", app="WhatsApp", process="WhatsApp.Root.exe"), S) == "WhatsApp"
    bank = win(2, "Chase Online - Accounts - Google Chrome", app="Google Chrome", process="chrome.exe")
    assert "tab" in privacy.sensitive_reason(bank, S)
    # The same words outside a browser (say, a file in an editor) are not blocked.
    assert privacy.sensitive_reason(win(3, "chase_bank.py - Visual Studio Code", process="Code.exe"), S) is None
    vault = win(4, "Bitwarden", app="Bitwarden", process="Bitwarden.exe")
    assert privacy.sensitive_reason(vault, S)
    assert privacy.sensitive_reason(win(5, "Project docs - Google Chrome", process="chrome.exe"), S) is None
    toast = win(6, "New notification", process="ShellExperienceHost.exe", is_app=False)
    assert privacy.sensitive_reason(toast, S) == "notification"


def test_reason_never_leaks_the_title():
    bank = win(2, "Chase - Balance $12,345.67 - Google Chrome", app="Google Chrome", process="chrome.exe")
    assert "12,345" not in privacy.sensitive_reason(bank, S)


def test_redaction_blacks_out_only_the_visible_part():
    monitors = [mon(1, 0, 0, 1000, 800)]
    editor = win(1, "notes", process="notepad.exe", rect=Rect(100, 100, 300, 300), z=0)  # in front, overlaps
    chat = win(2, "WhatsApp", app="WhatsApp", process="WhatsApp.exe", rect=Rect(200, 200, 400, 300), z=1)
    d = desk(monitors, [editor, chat])
    img = Image.new("RGB", (1000, 800), (255, 255, 255))
    hidden = privacy.redact(img, (0, 0), d, S)
    assert hidden == ["WhatsApp"]
    assert img.getpixel((500, 450)) == privacy.SHADE  # visible part of WhatsApp
    assert img.getpixel((250, 250)) == (255, 255, 255)  # covered by the editor, left alone
    assert img.getpixel((50, 50)) == (255, 255, 255)


def test_redaction_respects_capture_origin():
    monitors = [mon(1, -1920, 0, 1920, 1080)]
    chat = win(2, "Signal", app="Signal", process="Signal.exe", rect=Rect(-1000, 100, 200, 200), z=0)
    d = desk(monitors, [chat])
    img = Image.new("RGB", (1920, 1080), (255, 255, 255))
    privacy.redact(img, (-1920, 0), d, S)
    assert img.getpixel((925, 105)) == privacy.SHADE  # window's top-left corner, in image pixels
    assert img.getpixel((915, 105)) == (255, 255, 255)


def test_pause_and_resume_and_timed_pause():
    assert not privacy.pause_state().paused
    privacy.pause()
    assert privacy.pause_state().paused
    try:
        privacy.ensure_not_paused()
        raise AssertionError("should have raised")
    except Paused as exc:
        assert "paused" in str(exc)
    privacy.resume()
    assert not privacy.pause_state().paused
    privacy.pause(minutes=-1)  # already expired
    assert not privacy.pause_state().paused


def test_cleanup_deletes_only_old_files(argus_home):
    old = argus_home / "captures" / "2026-01-01" / "old.png"
    new = argus_home / "captures" / "2026-10-01" / "new.png"
    keep = argus_home / "baselines" / "login.png"
    for p in (old, new, keep):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    ancient = time.time() - 30 * 86400
    import os

    os.utime(old, (ancient, ancient))
    os.utime(keep, (ancient, ancient))
    assert privacy.cleanup(S, force=True) == 1
    assert not old.exists() and not old.parent.exists() and new.exists() and keep.exists()
