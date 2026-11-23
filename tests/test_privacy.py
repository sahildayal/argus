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


# ------------------------------------------------------------- work profile

import pytest  # noqa: E402

from argus.desktop import NO_HOST, ArgusError  # noqa: E402

WORK_TOML = r"""
[profile]
active = "work"
[work]
allow_apps = ["Code.exe", "Signal.exe"]
allow_browser_titles = ['\blocalhost\b']
"""
W = Settings.from_toml(WORK_TOML)
WHITE = (255, 255, 255)
MONS = [mon(1, 0, 0, 1000, 800)]
PHI = "Doe, John (MRN 123456) - Epic Hyperspace"


def wallpaper(z):
    return win(900, "Program Manager", process="explorer.exe", pid=4, cls="Progman", rect=Rect(0, 0, 1000, 800), z=z, is_app=False)


def taskbar(z):
    return win(901, "", process="explorer.exe", pid=4, cls="Shell_TrayWnd", rect=Rect(0, 760, 1000, 40), z=z, is_app=False)


def code(z=1, rect=Rect(100, 100, 400, 300)):
    return win(1, "app.tsx - web - Visual Studio Code", app="Visual Studio Code", process="Code.exe", rect=rect, z=z)


def epic(z=2, rect=Rect(600, 100, 300, 300)):
    return win(2, PHI, app="Epic", process="Hyperdrive.exe", rect=rect, z=z)


def white():
    return Image.new("RGB", (1000, 800), WHITE)


def test_work_profile_shows_only_allowed_windows():
    d = desk(MONS, [taskbar(0), code(1), epic(2), wallpaper(3)])
    img = white()
    hidden = privacy.redact(img, (0, 0), d, W)
    assert hidden == [privacy.WORK_HIDDEN]
    assert img.getpixel((200, 200)) == WHITE  # the allowed editor
    assert img.getpixel((700, 200)) == privacy.SHADE  # the unallowed app
    assert img.getpixel((50, 50)) == privacy.SHADE  # wallpaper
    assert img.getpixel((500, 780)) == privacy.SHADE  # taskbar


def test_work_profile_hides_areas_no_window_covers():
    d = desk(MONS, [code(0)])  # no wallpaper window at all
    img = white()
    privacy.redact(img, (0, 0), d, W)
    assert img.getpixel((200, 200)) == WHITE and img.getpixel((900, 700)) == privacy.SHADE


def test_shell_surfaces_stay_hidden_even_if_explorer_is_allowed():
    s = Settings.from_toml(WORK_TOML.replace('"Code.exe"', '"Code.exe", "explorer.exe"'))
    files = win(3, "Documents", app="File Explorer", process="explorer.exe", pid=4, cls="CabinetWClass", rect=Rect(100, 450, 300, 200), z=1)
    toast = win(4, "New notification", process="ShellExperienceHost.exe", rect=Rect(700, 600, 250, 100), z=0, is_app=False)
    d = desk(MONS, [toast, files, taskbar(2), wallpaper(3)])
    img = white()
    privacy.redact(img, (0, 0), d, s)
    assert img.getpixel((200, 500)) == WHITE  # a File Explorer window is fine
    assert img.getpixel((50, 50)) == privacy.SHADE and img.getpixel((500, 780)) == privacy.SHADE
    assert img.getpixel((800, 650)) == privacy.SHADE


def test_unallowed_window_covering_an_allowed_one_is_blacked_out():
    d = desk(MONS, [epic(0, Rect(300, 300, 200, 200)), code(1, Rect(100, 100, 600, 500)), wallpaper(2)])
    img = white()
    privacy.redact(img, (0, 0), d, W)
    assert img.getpixel((400, 400)) == privacy.SHADE  # the covering part
    assert img.getpixel((150, 150)) == WHITE  # the rest of the editor
    assert img.getpixel((650, 550)) == WHITE


def test_allowed_window_covering_an_unallowed_one_stays_visible():
    d = desk(MONS, [code(0, Rect(650, 150, 100, 100)), epic(1), wallpaper(2)])
    img = white()
    privacy.redact(img, (0, 0), d, W)
    assert img.getpixel((700, 200)) == WHITE and img.getpixel((620, 120)) == privacy.SHADE


def chrome(hwnd, title, z, rect, pid=50):
    return win(hwnd, f"{title} - Google Chrome", app="Google Chrome", process="chrome.exe", pid=pid, rect=rect, z=z)


def menu(hwnd, z, rect, pid=50, owner=0):
    return win(hwnd, "", process="chrome.exe", pid=pid, cls="Chrome_WidgetWin_1", rect=rect, z=z, is_app=False, owner=owner)


def test_allowed_apps_own_popups_stay_visible():
    page = chrome(10, "localhost:3000", 1, Rect(100, 100, 500, 400))
    dropdown = menu(11, 0, Rect(450, 150, 250, 200))  # spills past the window's edge
    d = desk(MONS, [dropdown, page, wallpaper(2)])
    img = white()
    privacy.redact(img, (0, 0), d, W)
    assert img.getpixel((200, 200)) == WHITE
    assert img.getpixel((650, 300)) == WHITE  # the part of the menu outside the window
    assert img.getpixel((800, 600)) == privacy.SHADE


def test_popups_are_hidden_when_they_could_belong_to_a_hidden_window():
    # Every Chrome window shares a process. With a hidden Chrome window open, an
    # unowned popup could be the hidden window's; an owned one is attributable.
    page = chrome(10, "localhost:3000", 2, Rect(100, 100, 500, 400))
    other = chrome(12, PHI, 3, Rect(0, 0, 1000, 760))
    unowned = menu(11, 0, Rect(450, 150, 250, 100))
    owned = menu(13, 1, Rect(150, 300, 100, 100), owner=10)
    d = desk(MONS, [unowned, owned, page, other, wallpaper(4)])
    img = white()
    privacy.redact(img, (0, 0), d, W)
    assert img.getpixel((200, 200)) == WHITE
    assert img.getpixel((650, 200)) == privacy.SHADE and img.getpixel((500, 200)) == privacy.SHADE
    assert img.getpixel((200, 350)) == WHITE
    assert img.getpixel((800, 600)) == privacy.SHADE  # the PHI window


def test_browser_window_is_allowed_by_its_active_tab_title():
    assert privacy.allowed(chrome(10, "localhost:3000 - Dashboard", 0, Rect(0, 0, 10, 10)), W)
    assert not privacy.allowed(chrome(10, "Inbox - Gmail", 0, Rect(0, 0, 10, 10)), W)
    # The browser-title list only applies to browsers.
    assert not privacy.allowed(win(5, "localhost notes", process="notepad.exe"), W)
    # Unknown processes are never allowed.
    assert not privacy.allowed(win(6, "localhost", process="", pid=0), W)


def test_block_list_still_wins_in_the_work_profile():
    signal = win(3, "Signal", app="Signal", process="Signal.exe", rect=Rect(100, 450, 300, 200), z=0)
    d = desk(MONS, [signal, code(1), wallpaper(2)])
    img = white()
    hidden = privacy.redact(img, (0, 0), d, W)
    assert "Signal" in hidden and privacy.WORK_HIDDEN in hidden
    assert img.getpixel((200, 500)) == privacy.SHADE and img.getpixel((200, 200)) == WHITE


def test_fully_allowed_capture_reports_nothing_hidden():
    img = Image.new("RGB", (100, 100), WHITE)
    assert privacy.redact(img, (150, 150), desk(MONS, [code(0), wallpaper(1)]), W) == []
    assert img.getpixel((50, 50)) == WHITE


def test_hidden_area_gets_one_label_not_one_per_window():
    d = desk(MONS, [code(0, Rect(0, 0, 200, 200)), epic(1, Rect(300, 300, 600, 400)), wallpaper(2)])
    img = white()
    privacy.redact(img, (0, 0), d, W)
    # Wallpaper and the hidden window form one block: one label, in its middle.
    assert len(img.crop((300, 380, 700, 420)).getcolors()) > 1  # label text on the shade
    assert img.crop((300, 560, 900, 700)).getcolors() == [(600 * 140, privacy.SHADE)]  # no second label


def test_personal_profile_is_unchanged():
    d = desk(MONS, [taskbar(0), code(1), epic(2), wallpaper(3)])
    img = white()
    assert privacy.redact(img, (0, 0), d, S) == []
    assert img.getpixel((700, 200)) == WHITE and img.getpixel((50, 50)) == WHITE
    assert privacy.display_name(epic(), S) == epic().label


def test_display_name_never_leaks_a_hidden_title():
    assert privacy.display_name(epic(), W) == "Epic (hidden, work profile)"
    assert "MRN" not in privacy.display_name(epic(), W)
    assert privacy.display_name(code(), W) == code().label
    signal = win(3, "Signal: Mom", app="Signal", process="Signal.exe")
    assert "Mom" not in privacy.display_name(signal, W)


# -- end to end through the service, with a fake desktop and fake pixels


@pytest.fixture
def work_session(argus_home, monkeypatch):
    from argus import capture, config, service

    argus_home.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(WORK_TOML, encoding="utf-8")
    monkeypatch.setattr(capture, "grab", lambda rect: Image.new("RGB", (rect.w, rect.h), WHITE))
    session = service.Session(host=NO_HOST, track=False)
    fake = {"desk": desk(MONS, [epic(0), code(1), wallpaper(2)], fg=2)}
    monkeypatch.setattr(session, "desk", lambda: fake["desk"])
    session.fake = fake
    return session


def test_list_screens_hides_titles_of_unallowed_windows(work_session):
    text = work_session.list_screens().parts[0]
    assert "MRN" not in text and "Doe" not in text
    assert "Epic (hidden, work profile)" in text
    assert "app.tsx" in text  # allowed windows keep their titles
    assert "work profile" in text.splitlines()[0]


def test_window_screenshot_of_unallowed_window_is_refused(work_session):
    with pytest.raises(ArgusError, match="isn't allowed by the user's work profile") as err:
        work_session.screenshot(window="0x2")
    assert "MRN" not in str(err.value)
    with pytest.raises(ArgusError, match="work profile"):
        work_session.screenshot(window="Epic", region=[0, 0, 50, 50])
    with pytest.raises(ArgusError, match="work profile"):
        work_session.screenshot(window="active")


def test_hidden_titles_cant_be_probed_by_window_selectors(work_session):
    with pytest.raises(ArgusError) as err:
        work_session.screenshot(window="MRN 123456")
    assert "No window matches" in str(err.value)
    assert "Doe" not in str(err.value) and "Epic (hidden, work profile)" in str(err.value)


def test_allowed_window_screenshot_works(work_session):
    (cap,) = work_session.screenshot(window="vs code").captures
    assert cap.shot.profile == "work" and "app.tsx" in cap.shot.label


def test_look_skips_unallowed_windows_and_says_so(work_session):
    result = work_session.look()
    text = "\n".join(p for p in result.parts if isinstance(p, str))
    assert "Visual Studio Code" in text and "Epic (hidden, work profile)" in text
    assert "MRN" not in text
    assert result.captures[0].shot.window.hwnd == 1


def test_look_with_nothing_allowed_explains(work_session):
    work_session.fake["desk"] = desk(MONS, [epic(0), wallpaper(1)], fg=2)
    with pytest.raises(ArgusError, match="No window the work profile allows") as err:
        work_session.look()
    assert "MRN" not in str(err.value)


def test_monitor_capture_in_work_profile_is_redacted(work_session):
    (cap,) = work_session.screenshot(monitor="1").captures
    assert cap.shot.hidden == [privacy.WORK_HIDDEN]
    assert cap.image.getpixel((700, 200)) == privacy.SHADE and cap.image.getpixel((200, 200)) == WHITE


def test_monitor_and_region_trust_only_visible_windows_in_capture(work_session):
    editor = code(1, Rect(50, 50, 400, 300))
    covered = win(3, "Covered", process="Code.exe", rect=Rect(200, 100, 40, 40), z=2)
    same_monitor = win(4, "Other project", process="Code.exe", rect=Rect(550, 50, 350, 300), z=3)
    other_monitor = win(5, "Other monitor", process="Code.exe", rect=Rect(-350, 50, 300, 300), z=4)
    # The front window overlaps the region and hides the only bit of Other
    # project that intersects it. That project is still visible on the monitor.
    front = epic(0, Rect(550, 50, 100, 250))
    monitors = [*MONS, mon(2, -1000, 0, 1000, 800)]
    work_session.fake["desk"] = desk(monitors, [front, editor, covered, same_monitor, other_monitor])
    monitor = work_session.screenshot(monitor="1").captures[0].shot
    region = work_session.screenshot(region=[0, 0, 650, 250]).captures[0].shot
    assert monitor.share_windows == privacy.named_windows([editor, same_monitor], W)
    assert region.share_windows == privacy.named_windows([editor], W)


def test_window_scope_has_target_and_overlap_but_excludes_other_matches(work_session, monkeypatch):
    from argus import capture

    target = code(1, Rect(100, 100, 400, 300))
    overlap = win(4, "Overlap localhost", process="chrome.exe", rect=Rect(320, 100, 350, 300), z=0)
    outside = win(5, "Other Visual Studio Code project", process="Code.exe", rect=Rect(700, 50, 250, 200), z=2)
    work_session.fake["desk"] = desk(MONS, [overlap, target, outside])
    monkeypatch.setattr(capture, "render_window", lambda w: Image.new("RGB", (w.rect.w, w.rect.h), WHITE))
    shot = work_session.screenshot(window="Visual Studio Code").captures[0].shot
    assert shot.method != "printwindow"  # the work profile never renders offscreen
    assert shot.share_windows == privacy.named_windows([target, overlap], W)
    assert any("Other Visual Studio Code project" in note for note in shot.notes)
    assert not shot.allow_trust


def test_captures_from_the_personal_profile_are_not_reshown(work_session):
    from argus.capture import Shot
    from argus.imaging import render

    shot = Shot(image=Image.new("RGB", (400, 300), WHITE), kind="monitor", label="Monitor 1", origin=(0, 0), target={},
                profile="personal")
    view, _ = render(shot.image, max_side=2000)
    cap = work_session.store.add(shot, view, save=False)
    with pytest.raises(ArgusError, match="wasn't taken under it"):
        work_session.zoom(box=[0, 0, 100, 100], capture_id=cap.id)


def test_latest_snip_skips_the_screenshots_folder_in_work_profile(work_session, monkeypatch):
    from argus import service

    seen = {}

    def fake_latest(count, clip_time, clip_seq, include_files=True):
        seen["include_files"] = include_files
        return []

    monkeypatch.setattr(service, "latest_snips", fake_latest)
    with pytest.raises(ArgusError, match="clipboard"):
        work_session.latest_snip()
    assert seen["include_files"] is False
