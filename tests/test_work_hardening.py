"""Regression tests for the work-profile security review: each one pins a way
the work profile could have shown (or leaked the name of) something it hides."""

import os
import time

import pytest
from PIL import Image

from argus import capture, config, desktop, privacy, seal, snips, testing, win32
from argus.capture import ScreenChanged, Shot
from argus.config import DEFAULT_CONFIG, Settings
from argus.desktop import NO_HOST, ArgusError, PolicyChanged
from argus.geometry import Rect
from argus.store import open_file, replayable, write_png

from conftest import desk, mon, win

WORK_TOML = r"""
[profile]
active = "work"
[work]
allow_apps = ["Code.exe", "Signal.exe"]
allow_browser_titles = ['\blocalhost\b']
"""
W = Settings.from_toml(WORK_TOML)
P = Settings.from_toml(DEFAULT_CONFIG)
WHITE = (255, 255, 255)
SHADE = privacy.SHADE
MONS = [mon(1, 0, 0, 1000, 800)]
PHI = "Doe, John (MRN 123456) - Epic Hyperspace"


def wallpaper(z):
    return win(900, "Program Manager", process="explorer.exe", pid=4, cls="Progman", rect=Rect(0, 0, 1000, 800), z=z, is_app=False)


def code(z=1, rect=Rect(100, 100, 400, 300), **kw):
    return win(1, "app.tsx - web - Visual Studio Code", app="Visual Studio Code", process="Code.exe", rect=rect, z=z, **kw)


def epic(z=2, rect=Rect(600, 100, 300, 300)):
    return win(2, PHI, app="Epic", process="Hyperdrive.exe", rect=rect, z=z)


def chrome(hwnd, title, z, rect, pid=50, **kw):
    return win(hwnd, f"{title} - Google Chrome", app="Google Chrome", process="chrome.exe", pid=pid, rect=rect, z=z, **kw)


def popup(hwnd, z, rect, pid=50, owner=0, process="chrome.exe", **kw):
    return win(hwnd, "", process=process, pid=pid, cls="Chrome_WidgetWin_1", rect=rect, z=z, is_app=False, owner=owner, **kw)


def white(size=(1000, 800)):
    return Image.new("RGB", size, WHITE)


@pytest.fixture
def grabs(monkeypatch):
    calls: list[Rect] = []

    def fake(rect):
        calls.append(rect)
        return Image.new("RGB", (rect.w, rect.h), WHITE)

    monkeypatch.setattr(capture, "grab", fake)
    return calls


def write_config(text: str) -> None:
    path = config.config_path()
    path.write_text(text, encoding="utf-8")
    future = time.time() + 10 + len(text) % 7  # a distinct mtime so settings() reloads
    os.utime(path, (future, future))


@pytest.fixture
def work_session(argus_home, monkeypatch, grabs):
    from argus import service

    argus_home.mkdir(parents=True, exist_ok=True)
    write_config(WORK_TOML)
    session = service.Session(host=NO_HOST, track=False)
    fake = {"desk": desk(MONS, [epic(0), code(1), wallpaper(2)], fg=2)}
    monkeypatch.setattr(session, "desk", lambda: fake["desk"])
    session.fake = fake
    return session


# ---------------------------------------------- 1. snapshot / grab race


def test_work_capture_retries_until_the_windows_hold_still(grabs):
    moved = desk(MONS, [epic(0, Rect(100, 100, 300, 300)), code(1), wallpaper(2)])  # PHI now covers the editor
    first = desk(MONS, [code(0), epic(1), wallpaper(2)], refresh=lambda: moved)
    shot = capture.capture_monitor(first, MONS[0], W)
    assert len(grabs) == 2
    assert shot.image.getpixel((200, 200)) == SHADE  # redacted against the layout that held still


def test_work_capture_refuses_when_the_screen_keeps_changing(grabs):
    state = {}
    a = desk(MONS, [code(0), wallpaper(1)], refresh=lambda: state["b"])
    state["b"] = desk(MONS, [code(0, Rect(110, 100, 400, 300)), wallpaper(1)], refresh=lambda: a)
    with pytest.raises(ScreenChanged, match="kept changing"):
        capture.capture_region(a, Rect(0, 0, 500, 500), W)
    assert len(grabs) == capture.GRAB_ATTEMPTS == 3


def test_title_change_that_flips_the_verdict_forces_a_retry(grabs):
    later = desk(MONS, [chrome(10, "Inbox - Gmail", 0, Rect(100, 100, 400, 300)), wallpaper(1)])
    first = desk(MONS, [chrome(10, "localhost:3000", 0, Rect(100, 100, 400, 300)), wallpaper(1)], refresh=lambda: later)
    shot = capture.capture_monitor(first, MONS[0], W)
    assert len(grabs) == 2 and shot.image.getpixel((200, 200)) == SHADE


def test_window_capture_follows_a_moving_window(work_session, grabs):
    moved = desk(MONS, [epic(0), code(1, Rect(150, 120, 400, 300)), wallpaper(2)], fg=2)
    work_session.fake["desk"] = desk(MONS, [epic(0), code(1), wallpaper(2)], fg=2, refresh=lambda: moved)
    (cap,) = work_session.screenshot(window="vs code").captures
    assert len(grabs) == 2 and cap.shot.origin == (150, 120)


def test_personal_profile_never_resnapshots(grabs):
    d = desk(MONS, [code(0), wallpaper(1)], refresh=lambda: pytest.fail("personal captures must not re-snapshot"))
    capture.capture_monitor(d, MONS[0], P)
    assert len(grabs) == 1


# ------------------------------------- 2. see-through, DPI, corners, bounds


def test_see_through_overlay_is_an_occluder_in_work_only():
    overlay = win(7, "", process="overlay.exe", rect=Rect(150, 150, 100, 100), z=0, is_app=False, see_through=True)
    layers = [overlay, code(1), wallpaper(2)]
    img = white()
    hidden = privacy.redact(img, (0, 0), desk(MONS, layers), W)
    assert img.getpixel((200, 200)) == SHADE and "see-through overlay windows" in hidden
    assert img.getpixel((400, 300)) == WHITE
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, layers), P)
    assert img.getpixel((200, 200)) == WHITE  # personal profile: unchanged


def test_provably_transparent_overlay_is_ignored():
    ghost = win(7, "", process="overlay.exe", rect=Rect(150, 150, 100, 100), z=0, is_app=False, see_through=True, alpha=0)
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [ghost, code(1), wallpaper(2)]), W)
    assert img.getpixel((200, 200)) == WHITE


def test_work_capture_refused_without_per_monitor_dpi_awareness(grabs, monkeypatch):
    monkeypatch.setattr(win32, "DPI_AWARENESS", "system")
    with pytest.raises(ArgusError, match="DPI aware"):
        capture.capture_monitor(desk(MONS, [code(0)]), MONS[0], W)
    assert not grabs
    capture.capture_monitor(desk(MONS, [code(0)]), MONS[0], P)  # personal is unaffected


def test_window_with_an_inexact_rect_stays_hidden():
    img = white()
    hidden = privacy.redact(img, (0, 0), desk(MONS, [code(0, rect_exact=False), wallpaper(1)]), W)
    assert img.getpixel((200, 200)) == SHADE and any("exact position" in h for h in hidden)


@pytest.mark.parametrize("dpi, corner_px", [(96, 2), (144, 3)])
def test_rounded_corners_of_allowed_windows_stay_hidden(dpi, corner_px):
    monitors = [mon(1, 0, 0, 1000, 800, dpi=dpi)]
    img = white()
    privacy.redact(img, (0, 0), desk(monitors, [code(0), epic(1, Rect(0, 0, 1000, 800))]), W)
    x, y = 100, 100  # the editor's top-left corner, over the hidden window
    assert img.getpixel((x + corner_px, y + corner_px)) == SHADE
    assert img.getpixel((x + 20, y + 20)) == WHITE and img.getpixel((x + 200, y + 1)) == WHITE


def test_maximized_windows_are_not_rounded():
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [code(0, rect=Rect(0, 0, 1000, 760), maximized=True), wallpaper(1)]), W)
    assert img.getpixel((0, 0)) == WHITE


def test_reveals_are_clamped_to_physical_monitors():
    # The editor hangs off the right edge of the only monitor; the image is wider.
    img = white((1200, 800))
    privacy.redact(img, (0, 0), desk(MONS, [code(0, Rect(800, 100, 400, 300))]), W)
    assert img.getpixel((900, 200)) == WHITE and img.getpixel((1100, 200)) == SHADE


# ------------------------------------------------ 3. popup attribution


def test_owned_popup_of_a_hidden_window_is_hidden_even_in_an_allowed_process():
    page = chrome(10, "localhost:3000", 2, Rect(100, 100, 500, 400))
    phi = chrome(12, PHI, 3, Rect(550, 100, 400, 400))
    menu = popup(13, 0, Rect(150, 150, 100, 100), owner=12)  # over the allowed page, but the PHI window's
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [menu, page, phi, wallpaper(4)]), W)
    assert img.getpixel((200, 200)) == SHADE and img.getpixel((400, 400)) == WHITE


def test_popup_chain_needs_every_link_revealed():
    page = chrome(10, "localhost:3000", 3, Rect(100, 100, 500, 400))
    sub = popup(11, 1, Rect(150, 150, 100, 100), owner=10)
    subsub = popup(12, 0, Rect(300, 150, 100, 100), owner=11)
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [subsub, sub, page, wallpaper(4)]), W)
    assert img.getpixel((200, 200)) == WHITE and img.getpixel((350, 200)) == WHITE
    # A link that's hidden (here: another process) breaks the chain behind it.
    sub = popup(11, 1, Rect(150, 150, 100, 100), owner=10, pid=77, process="injector.exe")
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [subsub, sub, page, wallpaper(4)]), W)
    assert img.getpixel((200, 200)) == SHADE and img.getpixel((350, 200)) == SHADE


def test_unowned_popup_hidden_while_its_process_has_a_minimized_hidden_window():
    page = chrome(10, "localhost:3000", 1, Rect(100, 100, 500, 400))
    minimized = chrome(12, PHI, 5, Rect(-32000, -32000, 160, 28), minimized=True)
    menu = popup(11, 0, Rect(150, 150, 100, 100))
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [menu, page, wallpaper(2), minimized]), W)
    assert img.getpixel((200, 200)) == SHADE


UWP_HOST = 300


def uwp(hwnd, title, *, app_pid, process, z, rect, attributed=True):
    return win(hwnd, title, app="Calculator", process=process, pid=UWP_HOST, cls="ApplicationFrameWindow", rect=rect, z=z,
               app_pid=app_pid, attributed=attributed)


def test_uwp_frames_are_attributed_by_the_hosted_app_not_the_frame_host():
    s = Settings.from_toml(WORK_TOML.replace('"Code.exe"', '"Code.exe", "CalculatorApp.exe", "ApplicationFrameHost.exe"'))
    calc = uwp(20, "Calculator", app_pid=301, process="CalculatorApp.exe", z=2, rect=Rect(100, 100, 300, 300))
    unknown = uwp(21, "Something", app_pid=0, process="ApplicationFrameHost.exe", z=3, rect=Rect(500, 100, 300, 300),
                  attributed=False)
    host_popup = popup(22, 0, Rect(150, 150, 50, 50), pid=UWP_HOST, process="ApplicationFrameHost.exe")
    app_popup = popup(23, 1, Rect(250, 150, 50, 50), pid=301, process="CalculatorApp.exe")
    assert privacy.allowed(calc, s) and not privacy.allowed(unknown, s)
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [host_popup, app_popup, calc, unknown, wallpaper(4)]), s)
    assert img.getpixel((200, 300)) == WHITE  # the app itself
    assert img.getpixel((600, 200)) == SHADE  # a frame whose app couldn't be identified
    assert img.getpixel((170, 170)) == SHADE  # a popup of the frame host: can't be tied to Calculator
    assert img.getpixel((270, 170)) == WHITE  # Calculator's own popup (its process, allowed by exe name)


def test_unreadable_process_is_never_allowed():
    assert not privacy.allowed(win(5, "app.tsx - Visual Studio Code", process="", pid=1234), W)


def test_ambiguous_uwp_host_is_not_attributed(monkeypatch):
    monkeypatch.setattr(win32, "child_windows", lambda h: [1, 2])
    monkeypatch.setattr(win32, "window_pid", lambda h: {1: 401, 2: 402}[h])
    monkeypatch.setattr(desktop, "_process", lambda pid: {401: ("a.exe", ""), 402: ("b.exe", "")}[pid])
    monkeypatch.setattr(win32, "file_description", lambda exe: "")
    process, _app, pid, attributed = desktop._friendly_app(99, UWP_HOST, "ApplicationFrameHost.exe", "")
    assert not attributed and (process, pid) == ("a.exe", 401)  # the block list still sees the first app
    monkeypatch.setattr(win32, "child_windows", lambda h: [1])
    assert desktop._friendly_app(99, UWP_HOST, "ApplicationFrameHost.exe", "")[3]


def test_process_cache_survives_pid_reuse(monkeypatch):
    life = {"start": 1.0, "name": "Code.exe"}

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return life["start"]

        def name(self):
            return life["name"]

        def exe(self):
            return ""

    monkeypatch.setattr(desktop.psutil, "Process", FakeProcess)
    monkeypatch.setattr(desktop, "_proc_cache", {})
    assert desktop._process(4242)[0] == "Code.exe"
    life.update(start=2.0, name="Hyperdrive.exe")  # same PID, different program
    assert desktop._process(4242)[0] == "Hyperdrive.exe"


# ------------------------------------------------------ 4. config fails closed


@pytest.mark.parametrize(
    "text",
    [
        WORK_TOML.replace('active = "work"', 'active = "wrok"'),  # unknown profile name
        WORK_TOML + "allow_titles = 'patient'\n",  # a string, not a list
        WORK_TOML + 'allow_snip_folder = "false"\n',  # a string, not a bool
        WORK_TOML + "allow_app = ['Epic.exe']\n",  # unknown key (typo)
        WORK_TOML + "[privacy]\nredact_titles = ['(oops']\n",  # a block rule that didn't compile
        WORK_TOML + "[privacy]\nredact_apps = 'Signal.exe'\n",
        WORK_TOML.replace('active = "work"', "active = 1"),
        WORK_TOML.replace('active = "work"', 'actve = "work"'),  # typo'd key in [profile]
        'profile = "work"\n[work]\nallow_apps = ["Code.exe"]\n',  # not a table
        WORK_TOML.replace("[work]", "[work"),  # doesn't parse
    ],
)
def test_config_mistakes_mean_work_with_nothing_allowed(text):
    s = Settings.from_toml(text)
    assert s.work and s.problems
    assert not s.allow_apps and not s.allow_browser_titles and not s.allow_titles and not s.allow_snip_folder


def test_config_problems_never_echo_values():
    s = Settings.from_toml(WORK_TOML + "allow_titles = 'Doe, John'\n")
    assert not any("Doe" in p for p in s.problems)


def test_personal_mistakes_outside_profile_stay_personal_and_unchanged():
    s = Settings.from_toml("[capture]\nmax_side = 'big'\n")
    assert not s.work and s.max_side == 2000 and s.problems
    s = Settings.from_toml("[privacy]\nredact_apps = 'Signal.exe'\n")
    assert not s.work and "whatsapp.exe" in s.redact_apps  # the default list, not a set of letters


def test_unreadable_config_file_locks_the_work_profile(argus_home):
    argus_home.mkdir(parents=True, exist_ok=True)
    config.config_path().write_bytes(b'[profile]\nactive = "personal"\n\xff\xfe')
    s = config.settings()
    assert s.work and not s.allow_apps and "can't be read" in s.problems[0]


def test_config_problems_show_in_status(argus_home):
    from argus.service import status_text

    argus_home.mkdir(parents=True, exist_ok=True)
    write_config(WORK_TOML + "allow_titles = 'x'\n")
    assert "Config problem: work.allow_titles" in status_text()


def test_policy_tracks_the_lists_that_decide_visibility():
    assert W.policy == Settings.from_toml(WORK_TOML).policy
    assert W.policy != Settings.from_toml(WORK_TOML.replace('"Signal.exe"', '"Signal.exe", "Epic.exe"')).policy
    assert W.policy != Settings.from_toml(WORK_TOML + "[privacy]\nredact_apps = []\n").policy
    assert W.policy != P.policy
    assert W.policy == Settings.from_toml(WORK_TOML + "[capture]\nmax_side = 1500\n").policy


# ------------------------------------------------------------ 5. replay trust


def _shot(profile="work", policy=None, colour="white"):
    return Shot(image=Image.new("RGB", (320, 200), colour), kind="monitor", label="Monitor 1", origin=(0, 0),
                target={"monitor": "1"}, profile=profile, policy=W.policy if policy is None else policy)


def test_seal_key_is_created_once_and_reused(argus_home):
    k = seal.key()
    assert len(k) == 32 and (argus_home / "seal.key").read_bytes() == k
    seal._keys.clear()
    assert seal.key() == k


def test_saved_captures_are_authenticated(argus_home, tmp_path):
    path = tmp_path / "c.png"
    write_png(_shot().image, path, {"profile": "work", "policy": W.policy, "label": "x"})
    assert open_file(path).policy == W.policy and replayable(open_file(path), W)


def test_forged_or_edited_work_tags_are_not_trusted(argus_home, tmp_path):
    from PIL import PngImagePlugin

    forged = tmp_path / "forged.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("argus", '{"profile": "work", "policy": "%s"}' % W.policy)
    info.add_text("argus-seal", "0" * 64)
    Image.new("RGB", (50, 50), "red").save(forged, pnginfo=info)
    shot = open_file(forged)
    assert shot.profile == "work" and shot.policy == "" and not replayable(shot, W)

    # A genuine capture whose pixels were edited afterwards loses its seal too.
    real = tmp_path / "real.png"
    write_png(Image.new("RGB", (50, 50), "white"), real, {"profile": "work", "policy": W.policy})
    with Image.open(real) as im:
        im.load()
        text = dict(im.text)
        edited = im.convert("RGB")
    edited.putpixel((10, 10), (0, 0, 0))
    info = PngImagePlugin.PngInfo()
    for k, v in text.items():
        info.add_text(k, v)
    edited.save(real, pnginfo=info)
    assert open_file(real).policy == ""


def test_forged_file_cant_be_zoomed_in_work_profile(work_session, tmp_path):
    from PIL import PngImagePlugin

    forged = tmp_path / "forged.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("argus", '{"profile": "work"}')
    Image.new("RGB", (300, 300), "red").save(forged, pnginfo=info)
    with pytest.raises(ArgusError, match="can vouch for"):
        work_session.zoom(box=[0, 0, 100, 100], capture_id=str(forged))


def test_captures_from_before_the_policy_changed_are_not_reshown(work_session):
    (cap,) = work_session.screenshot(monitor="1").captures
    work_session.zoom(box=[0, 0, 100, 100], capture_id=cap.id)  # fine under the same policy
    work_session.store.flush()
    write_config(WORK_TOML.replace('"Signal.exe"', '"Signal.exe", "Epic.exe"'))
    with pytest.raises(ArgusError, match="current settings"):
        work_session.zoom(box=[0, 0, 100, 100], capture_id=cap.id)
    with pytest.raises(ArgusError, match="current settings"):
        work_session.zoom(box=[0, 0, 100, 100], capture_id=str(cap.path))


def test_sealed_capture_reopens_by_path_under_the_same_policy(work_session):
    (cap,) = work_session.screenshot(monitor="1").captures
    work_session.store.flush()
    (z,) = work_session.zoom(box=[0, 0, 100, 100], capture_id=str(cap.path)).captures
    assert z.shot.policy == W.policy


def test_wait_for_stops_when_the_profile_changes_mid_call(work_session, monkeypatch):
    import asyncio

    calls = {"n": 0}

    def grab(rect):
        calls["n"] += 1
        if calls["n"] == 2:
            write_config(WORK_TOML.replace('"Signal.exe"', '"Signal.exe", "Epic.exe"'))
        return Image.new("RGB", (rect.w, rect.h), WHITE)

    monkeypatch.setattr(capture, "grab", grab)
    with pytest.raises(PolicyChanged, match="discarded"):
        asyncio.run(work_session.wait_for("change", monitor="1", timeout=5))


def test_wait_loop_lets_policy_changes_through():
    import asyncio

    calls = {"n": 0}

    async def grab():
        calls["n"] += 1
        if calls["n"] > 1:
            raise PolicyChanged("changed")
        return Shot(image=Image.new("RGB", (10, 10)), kind="region", label="t", origin=(0, 0), target={})

    async def nosleep(_s):
        await asyncio.sleep(0)

    with pytest.raises(PolicyChanged):
        asyncio.run(testing.wait_for(grab, "change", timeout=5, sleep=nosleep))


# --------------------------------------------- 6. baselines, metadata, errors


def test_baselines_from_outside_the_work_policy_are_counted_not_named(argus_home, capsys):
    from argus.cli import main

    argus_home.mkdir(parents=True, exist_ok=True)
    testing.save_baseline("doe-john-chart", _shot(profile="personal", policy=P.policy))
    write_config(WORK_TOML)
    testing.save_baseline("dashboard", _shot())
    assert main(["baseline", "list"]) == 0
    out = capsys.readouterr().out
    assert "doe-john" not in out and "dashboard" in out and "1 hidden baseline" in out
    with pytest.raises(ArgusError) as err:
        testing.load_baseline("nope", config.settings())
    assert "doe-john" not in str(err.value) and "dashboard" in str(err.value) and "1 more hidden" in str(err.value)


def test_cli_errors_are_scrubbed_in_work_profile(argus_home, capsys, monkeypatch):
    from argus import cli

    argus_home.mkdir(parents=True, exist_ok=True)
    write_config(WORK_TOML)

    def boom(args):
        raise KeyError(PHI)

    monkeypatch.setattr(cli, "_run", boom)
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "MRN" not in err and "error id" in err

    def missing(args):
        raise ArgusError(f"No such file: {argus_home / 'captures' / 'x_doe-john.png'}")

    monkeypatch.setattr(cli, "_run", missing)
    assert cli.main(["list"]) == 2
    err = capsys.readouterr().err
    assert "doe-john" not in err and "<Argus data folder>" in err


# ----------------------------------------------- 7. PrintWindow, clipboard


def test_work_profile_never_uses_printwindow(work_session, grabs, monkeypatch):
    monkeypatch.setattr(capture, "render_window", lambda w: pytest.fail("PrintWindow used in the work profile"))
    cover = epic(0, Rect(300, 300, 300, 200))  # covers part of the editor
    work_session.fake["desk"] = desk(MONS, [cover, code(1), wallpaper(2)], fg=1)
    (cap,) = work_session.screenshot(window="vs code").captures
    assert cap.shot.method == "screen" and len(grabs) == 1
    assert cap.image.getpixel((300, 250)) == SHADE  # the covered part (window-local coords)
    assert cap.image.getpixel((50, 50)) == WHITE
    assert any("covered" in n for n in cap.shot.notes)


def test_clipboard_only_mode_ignores_copied_files(tmp_path, monkeypatch):
    p = tmp_path / "chart.png"
    Image.new("RGB", (20, 20), "red").save(p)
    monkeypatch.setattr(snips.ImageGrab, "grabclipboard", lambda: [str(p)])
    assert snips.clipboard_image(files=False) is None
    assert snips.clipboard_image() is not None  # personal / snip-folder mode: unchanged
    assert snips.latest(1, folder=tmp_path / "none", include_files=False) == []


# ----------------------------------------------------------- 8. edge cases


def test_set_profile_wont_edit_a_profile_header_inside_a_string(argus_home):
    argus_home.mkdir(parents=True, exist_ok=True)
    text = 'notes = """\n[profile]\nactive = "personal"\n"""\n[capture]\nmax_side = 1000\n'
    config.config_path().write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="safely"):
        config.set_profile("work")
    assert config.config_path().read_text(encoding="utf-8") == text


def test_set_profile_appends_with_the_files_line_endings(argus_home):
    argus_home.mkdir(parents=True, exist_ok=True)
    path = config.config_path()
    path.write_bytes(b"[capture]\r\nmax_side = 1000\r\n")
    assert config.set_profile("work").work
    assert path.read_bytes() == b'[capture]\r\nmax_side = 1000\r\n\r\n[profile]\r\nactive = "work"\r\n'


def test_set_profile_refuses_duplicate_keys(argus_home):
    argus_home.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text('[profile]\nactive = "personal"\nactive = "work"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="valid TOML"):
        config.set_profile("work")


def test_look_skips_allowed_windows_that_the_block_list_hides(work_session):
    signal = win(3, "Signal: Mom", app="Signal", process="Signal.exe", rect=Rect(100, 450, 300, 200), z=0)
    work_session.fake["desk"] = desk(MONS, [signal, code(1), wallpaper(2)], fg=3)
    result = work_session.look()
    assert result.captures[0].shot.window.hwnd == 1
    assert "Mom" not in "\n".join(p for p in result.parts if isinstance(p, str))


def test_recapture_only_considers_shown_windows(grabs):
    target = {"window": 999, "title": PHI, "process": "chrome.exe"}
    phi = chrome(12, PHI, 1, Rect(500, 100, 400, 400), pid=50)
    page = chrome(10, "localhost:3000", 0, Rect(100, 100, 300, 300), pid=50)
    shot = capture.recapture(desk(MONS, [page, phi, wallpaper(2)]), W, target)
    assert shot.window.hwnd == 10
    with pytest.raises(ArgusError, match="no allowed chrome.exe window") as err:
        capture.recapture(desk(MONS, [phi, wallpaper(2)]), W, target)
    assert "MRN" not in str(err.value)
