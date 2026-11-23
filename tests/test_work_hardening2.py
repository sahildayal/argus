"""Regression tests for the second work-profile security review: each one pins
a way the work profile could have revealed, kept or leaked something it hides."""

import json
import time

import pytest
from PIL import Image

from argus import capture, config, errors, privacy, seal, win32
from argus.config import Settings
from argus.desktop import ArgusError
from argus.geometry import Rect
from argus.store import open_file, replayable

from conftest import desk, win
from test_work_hardening import (
    MONS, PHI, SHADE, WHITE, WORK_TOML, W, _shot, chrome, code, epic, grabs, popup, wallpaper, white, work_session,
    write_config,
)

__all__ = ["grabs", "work_session"]  # fixtures re-used from the first round


# ------------------------------------------------- 1. mask comparison


def test_maximize_between_snapshots_forces_a_retry(grabs):
    later = desk(MONS, [code(0, rect=Rect(0, 0, 1000, 760), maximized=True), wallpaper(1)])
    first = desk(MONS, [code(0, rect=Rect(0, 0, 1000, 760)), wallpaper(1)], refresh=lambda: later)
    shot = capture.capture_monitor(first, MONS[0], W)
    assert len(grabs) == 2 and shot.image.getpixel((0, 0)) == WHITE  # redacted as maximized: square corners


def test_the_mask_itself_is_compared_not_just_listed_fields(grabs, monkeypatch):
    monkeypatch.setattr(capture, "_layout", lambda desk, area, s: ())  # pretend no listed field changed
    moved = desk(MONS, [code(0, Rect(300, 300, 400, 300)), wallpaper(1)])
    first = desk(MONS, [code(0), wallpaper(1)], refresh=lambda: moved)
    shot = capture.capture_monitor(first, MONS[0], W)
    assert len(grabs) == 2 and shot.image.getpixel((150, 150)) == SHADE  # the old spot is hidden again


def test_owner_change_between_snapshots_forces_a_retry(grabs):
    page = chrome(10, "localhost:3000", 2, Rect(100, 100, 500, 400))
    phi = chrome(12, PHI, 3, Rect(600, 100, 300, 400))
    rehomed = desk(MONS, [popup(13, 0, Rect(150, 150, 100, 100), owner=12), page, phi, wallpaper(4)])
    first = desk(MONS, [popup(13, 0, Rect(150, 150, 100, 100), owner=10), page, phi, wallpaper(4)],
                 refresh=lambda: rehomed)
    shot = capture.capture_monitor(first, MONS[0], W)
    assert len(grabs) == 2 and shot.image.getpixel((200, 200)) == SHADE


def test_mask_matches_what_redaction_paints():
    d = desk(MONS, [epic(0), code(1), wallpaper(2)])
    region = Rect(0, 0, 1000, 800)
    mask, _, _ = privacy.work_mask(region, d, W)
    img = white()
    privacy.redact(img, (0, 0), d, W)
    for xy in [(200, 200), (700, 200), (50, 50), (900, 700)]:
        assert (mask.getpixel(xy) == 255) == (img.getpixel(xy) != WHITE)


# ------------------------------------------- 2. layered windows, popups


@pytest.mark.parametrize(
    "layered, alpha, colorkey, visible",
    [
        (False, None, False, True),  # an ordinary window
        (True, 255, False, True),  # layered but provably opaque
        (True, None, False, False),  # per-pixel alpha (UpdateLayeredWindow): no evidence
        (True, 200, False, False),  # translucent
        (True, 255, True, False),  # colour-keyed holes
    ],
)
def test_allowed_layered_window_is_revealed_only_when_provably_opaque(layered, alpha, colorkey, visible):
    editor = code(0, layered=layered, alpha=alpha, colorkey=colorkey)
    img = white()
    hidden = privacy.redact(img, (0, 0), desk(MONS, [editor, epic(1, Rect(0, 0, 1000, 800))]), W)
    assert (img.getpixel((250, 250)) == WHITE) is visible
    assert any("translucent" in h for h in hidden) is (not visible)


def test_per_pixel_alpha_window_reads_as_not_opaque(monkeypatch):
    monkeypatch.setattr(win32, "GetLayeredWindowAttributes", lambda *a: 0)  # what UpdateLayeredWindow windows return
    assert win32.layered_attributes(1) == (None, False)
    assert not code(0, layered=True, alpha=None).opaque


def test_owned_window_of_an_allowed_exe_needs_a_valid_owner_chain():
    phi = epic(2, Rect(0, 0, 1000, 800))
    # Code.exe's own process, empty title, but owned by the hidden PHI window.
    stray = popup(30, 0, Rect(150, 150, 100, 100), pid=1, process="Code.exe", owner=2)
    dialog = win(31, "Save As", app="Visual Studio Code", process="Code.exe", pid=1, rect=Rect(400, 400, 200, 150),
                 z=1, owner=2)
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [stray, dialog, code(3), phi]), W)
    assert img.getpixel((200, 200)) == SHADE and img.getpixel((500, 470)) == SHADE
    shown = privacy.shown_windows(desk(MONS, [stray, dialog, code(3), phi]), W)
    assert 30 not in shown and 31 not in shown and 1 in shown


def test_owned_dialog_of_an_allowed_window_still_shows():
    dialog = win(31, "Save As", app="Visual Studio Code", process="Code.exe", pid=1, rect=Rect(150, 150, 200, 150),
                 z=0, owner=1)
    img = white()
    privacy.redact(img, (0, 0), desk(MONS, [dialog, code(1), wallpaper(2)]), W)
    assert img.getpixel((250, 220)) == WHITE


def test_invisible_owners_must_be_the_same_process():
    phi = epic(2, Rect(0, 0, 1000, 800))
    # The window's (invisible) owner belongs to another process: untrusted.
    foreign = code(0, owner=500, owner_chain=((500, 77),))
    assert 1 not in privacy.shown_windows(desk(MONS, [foreign, phi]), W)
    # An app's own hidden "parking" owner is fine.
    parked = code(0, owner=500, owner_chain=((500, 1),))
    assert 1 in privacy.shown_windows(desk(MONS, [parked, phi]), W)
    # A looping or too-deep chain (ends in (0, 0)) never is.
    looped = code(0, owner=500, owner_chain=((500, 1), (0, 0)))
    assert 1 not in privacy.shown_windows(desk(MONS, [looped, phi]), W)


# ------------------------------------------------------- 3. profile loss


@pytest.fixture
def home(argus_home):
    argus_home.mkdir(parents=True, exist_ok=True)
    return argus_home


def test_first_run_stays_personal_without_a_marker(home):
    assert not config.settings().work
    assert not config.profile_state_path().exists()


@pytest.mark.parametrize(
    "damage",
    ["delete", "", '[profile]\nactive = "personal"\n', "[capture]\nmax_side = 1000\n", '[profiles]\nactive = "personal"\n',
     "[profile]\n"],
)
def test_work_sticks_whatever_happens_to_config_toml(home, damage):
    config.set_profile("work")
    assert config.read_profile_state() == "work"
    if damage == "delete":
        config.config_path().unlink()
    else:
        write_config(damage)
    s = config.settings()
    assert s.work and "switched on" in s.profile_reason
    if damage.startswith("[profiles]"):
        assert s.lockdown and not s.allow_apps


def test_only_an_explicit_switch_turns_work_off(home):
    config.set_profile("work")
    s = config.set_profile("personal")
    assert not s.work and config.read_profile_state() == "personal"


def test_hand_edited_work_also_sticks(home):
    write_config(WORK_TOML)
    assert config.settings().work and config.read_profile_state() == "work"
    write_config(WORK_TOML.replace('active = "work"', 'active = "personal"'))
    s = config.settings()
    assert s.work and 'says active = "personal"' in s.profile_reason
    assert s.allow_apps == W.allow_apps  # a clean file still decides what's allowed


def test_switch_to_work_holds_even_when_config_toml_cant_be_edited(home):
    write_config("[profile\nactive = 'personal'\n")
    with pytest.raises(ValueError, match="valid TOML"):
        config.set_profile("work")
    assert config.settings().work and config.read_profile_state() == "work"
    with pytest.raises(ValueError):
        config.set_profile("personal")  # can't edit the file: work stays
    assert config.settings().work


def test_unreadable_marker_counts_as_work(home):
    config.profile_state_path().write_bytes(b"\xff\xfe garbage")
    assert config.settings().work


@pytest.mark.parametrize("section", ["profiles", "privcy", "wrok", "gates"])
def test_misspelt_critical_sections_lock_the_work_profile(section):
    s = Settings.from_toml(WORK_TOML + f"[{section}]\nx = 1\n")
    assert s.work and s.lockdown and not s.allow_apps
    assert any("misspelt" in p for p in s.problems)


def test_unrelated_unknown_sections_are_only_a_warning():
    s = Settings.from_toml(WORK_TOML + "[extras]\nx = 1\n")
    assert s.work and not s.lockdown and s.allow_apps
    assert any("unknown section" in p for p in s.problems)


def test_status_and_profile_command_say_why_work_is_on(home, capsys):
    from argus.cli import main
    from argus.service import status_text

    config.set_profile("work")
    write_config('[profile]\nactive = "personal"\n')
    assert "Why work:" in status_text() and "switched on" in status_text()
    assert main(["profile"]) == 0
    assert "Why work:" in capsys.readouterr().out


# ------------------------------------------------------------- 4. snips


def _fake_snips(monkeypatch, seen=None, image=None):
    from argus import service, snips

    def fake(count, clip_time, clip_seq, include_files=True, clipboard_files=None):
        if seen is not None:
            seen.update(include_files=include_files, clipboard_files=clipboard_files)
        if image is None:
            return []
        return [snips.Snip(image, "clipboard", None, time.time())]

    monkeypatch.setattr(service, "latest_snips", fake)


def test_clipboard_snips_are_never_stamped_as_work_captures(work_session, monkeypatch):
    _fake_snips(monkeypatch, image=Image.new("RGB", (40, 30), "red"))
    (cap,) = work_session.latest_snip().captures
    assert cap.shot.policy == "" and not replayable(cap.shot, config.settings())
    work_session.store.flush()
    reopened = open_file(cap.path)
    assert reopened.policy == "" and not replayable(reopened, config.settings())


def test_snip_folder_opt_in_doesnt_enable_clipboard_file_lists(work_session, monkeypatch):
    seen = {}
    _fake_snips(monkeypatch, seen)
    write_config(WORK_TOML + "allow_snip_folder = true\n")
    with pytest.raises(ArgusError):
        work_session.latest_snip()
    assert seen == {"include_files": True, "clipboard_files": False}
    write_config(WORK_TOML + "allow_clipboard_files = true\n")
    with pytest.raises(ArgusError):
        work_session.latest_snip()
    assert seen == {"include_files": False, "clipboard_files": True}


def test_locked_down_work_profile_refuses_latest_snip(work_session, monkeypatch):
    _fake_snips(monkeypatch, image=Image.new("RGB", (40, 30), "red"))
    write_config(WORK_TOML + "allow_titles = 'x'\n")  # a mistake in [work]: locked down
    assert config.settings().lockdown
    with pytest.raises(ArgusError, match="locked down"):
        work_session.latest_snip()


def test_clipboard_files_setting_is_part_of_the_schema_and_policy():
    s = Settings.from_toml(WORK_TOML + "allow_clipboard_files = true\n")
    assert s.allow_clipboard_files and not s.problems and s.policy != W.policy


# --------------------------------------------------- 5. seal, policy, marks


def test_deleting_the_key_rejects_every_seal(home):
    from argus.store import write_png

    path = home / "c.png"
    write_png(_shot().image, path, {"profile": "work", "policy": W.policy, "label": "Monitor 1"})
    assert open_file(path).policy == W.policy
    (home / "seal.key").unlink()
    assert open_file(path).policy == ""  # the key is re-read on every verification
    assert not (home / "seal.key").exists()  # and verifying never makes a new one


def test_unreadable_key_rejects_seals(home, monkeypatch):
    from argus.store import write_png

    path = home / "c.png"
    write_png(_shot().image, path, {"profile": "work", "policy": W.policy})
    real = seal.Path.read_bytes

    def denied(self):
        if self.name == "seal.key":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(seal.Path, "read_bytes", denied)
    assert open_file(path).policy == ""


def test_redaction_version_is_part_of_the_policy(monkeypatch):
    before = Settings.from_toml(WORK_TOML).policy
    monkeypatch.setattr(config, "REDACTION_VERSION", config.REDACTION_VERSION + 1)
    assert Settings.from_toml(WORK_TOML).policy != before


def test_mark_captions_come_from_the_sealed_png_not_the_sidecar(work_session):
    from argus.marks import marks_dir, save_mark

    s = config.settings()
    shot = _shot(policy=s.policy)
    shot.label = "Visual Studio Code on Monitor 1"
    shot.taken = time.time()
    mark = save_mark(shot, shot)
    sidecar = marks_dir() / f"{mark.id}.json"
    info = json.loads(sidecar.read_text(encoding="utf-8"))
    info["window"] = info["monitor"] = PHI  # unsigned: anyone can write this
    sidecar.write_text(json.dumps(info), encoding="utf-8")
    text = "\n".join(p for p in work_session.latest_mark().parts if isinstance(p, str))
    assert "MRN" not in text and "Visual Studio Code on Monitor 1" in text


# ------------------------------------------------------- 6. error text


NAME = "Scan [Doe, John] 2026.png"


def test_scrub_removes_whole_data_file_names_with_spaces_and_brackets(home):
    write_config(WORK_TOML)
    for text in [
        f"No such file: {home / 'captures' / NAME}",
        f"Can't open '{home / 'marks' / NAME}' (bad header)",
        f"failed at {home / 'captures' / 'Doe, John notes'}",  # no extension: to the end of the line
        f"{home / 'captures' / NAME}.part was not written",
    ]:
        out = errors.scrub(text)
        assert "Doe" not in out and "John" not in out and "<Argus data folder>" in out, out


def test_file_errors_never_name_the_file_in_work(home):
    write_config(WORK_TOML)
    with pytest.raises(ArgusError) as err:
        open_file(home / "elsewhere" / NAME)
    assert "Doe" not in str(err.value)
    bad = home / NAME
    bad.write_bytes(b"not an image")
    with pytest.raises(ArgusError) as err:
        open_file(bad)
    assert "Doe" not in str(err.value)


def test_file_errors_are_unchanged_in_personal(home):
    with pytest.raises(ArgusError, match="Doe"):
        open_file(home / "elsewhere" / NAME)


def test_compare_doesnt_echo_a_path_in_work(work_session, home):
    path = str(home / NAME)
    shot = _shot(policy=config.settings().policy)
    shot.target = {}
    from argus.store import write_png

    write_png(shot.image, home / NAME, {"profile": "work", "policy": config.settings().policy, "label": "x"})
    with pytest.raises(ArgusError) as err:
        work_session.compare(capture_a=path)
    assert "Doe" not in str(err.value)
