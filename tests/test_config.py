import pytest

from argus import config
from argus.config import DEFAULT_CONFIG, Settings, parse_hotkey, pretty_hotkey


def test_default_file_is_written_and_parsed(argus_home):
    s = config.settings()
    assert (argus_home / "config.toml").read_text(encoding="utf-8") == DEFAULT_CONFIG
    assert s.max_side == 2000 and s.retention_days == 7 and not s.problems
    assert "whatsapp.exe" in s.redact_apps and "chrome.exe" in s.browsers


def test_settings_reload_when_the_file_changes(argus_home):
    config.settings()
    path = argus_home / "config.toml"
    path.write_text(DEFAULT_CONFIG.replace("retention_days = 7", "retention_days = 2"), encoding="utf-8")
    import os
    import time

    future = time.time() + 5
    os.utime(path, (future, future))
    assert config.settings().retention_days == 2


def test_bad_values_are_reported_not_fatal():
    s = Settings.from_toml("this is = = not toml")
    assert s.problems and s.max_side == 2000
    s = Settings.from_toml("[privacy]\nredact_titles = ['(unclosed']\n")
    assert any("bad regex" in p for p in s.problems)


def test_partial_files_fall_back_to_defaults():
    s = Settings.from_toml("[capture]\nmax_side = 1568\n")
    assert s.max_side == 1568 and s.retention_days == 7 and s.redact_apps


@pytest.mark.parametrize(
    "spec, mods, vk",
    [("ctrl+alt+s", 0x2 | 0x1, ord("S")), ("Ctrl+Shift+F9", 0x2 | 0x4, 0x78), ("win+alt+p", 0x8 | 0x1, ord("P")), ("pause", 0, 0x13)],
)
def test_hotkeys(spec, mods, vk):
    assert parse_hotkey(spec) == (mods | 0x4000, vk)


def test_bad_hotkeys():
    for bad in ("s", "ctrl+", "hyper+s", "ctrl+alt+nope"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)
    assert pretty_hotkey("ctrl+alt+s") == "Ctrl+Alt+S"
