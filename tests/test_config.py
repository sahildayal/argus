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


# ----------------------------------------------------------------- profiles


def test_profile_defaults_to_personal_with_empty_allow_lists():
    s = Settings.from_toml(DEFAULT_CONFIG)
    assert s.profile == "personal" and not s.work and not s.problems
    assert not s.allow_apps and not s.allow_browser_titles and not s.allow_titles and not s.allow_snip_folder


def test_work_profile_settings_are_parsed():
    s = Settings.from_toml(
        '[profile]\nactive = "Work"\n[work]\nallow_apps = ["Code.exe"]\n'
        "allow_browser_titles = ['\\blocalhost\\b']\nallow_titles = ['^Jira']\nallow_snip_folder = true\n"
    )
    assert s.work and s.allow_apps == {"code.exe"} and s.allow_snip_folder
    assert s.allow_browser_titles[0].search("LOCALHOST:3000 - Google Chrome")
    assert s.allow_titles[0].search("Jira board")


def test_old_config_files_without_the_new_sections_still_work():
    s = Settings.from_toml("[capture]\nmax_side = 1568\n")
    assert s.profile == "personal" and not s.problems
    s = Settings.from_toml('[profile]\nactive = "work"\n')  # no [work] section: nothing allowed
    assert s.work and not s.allow_apps and not s.problems


def test_bad_allow_regex_is_reported():
    s = Settings.from_toml('[profile]\nactive = "work"\n[work]\nallow_browser_titles = ["(oops"]\n')
    assert s.work and not s.allow_browser_titles
    assert any("work.allow_browser_titles: bad regex" in p for p in s.problems)


def test_unknown_profile_fails_closed():
    s = Settings.from_toml('[profile]\nactive = "wrok"\n')
    assert s.work and any("unknown profile" in p for p in s.problems)


def test_broken_file_keeps_the_work_profile():
    s = Settings.from_toml('[profile]\nactive = "work"\n[work\nallow_apps = [\n')
    assert s.work and not s.allow_apps and s.problems
    assert not Settings.from_toml("not = = toml").work


def test_set_profile_edits_only_that_line(argus_home):
    config.settings()  # writes the default file
    path = argus_home / "config.toml"
    original = path.read_text(encoding="utf-8")
    assert config.set_profile("work").work
    changed = path.read_text(encoding="utf-8")
    assert changed == original.replace('active = "personal"', 'active = "work"')
    assert not config.set_profile("personal").work
    assert path.read_text(encoding="utf-8") == original


def test_set_profile_keeps_comments_and_adds_a_missing_section(argus_home):
    argus_home.mkdir(parents=True, exist_ok=True)
    path = argus_home / "config.toml"
    path.write_text("# my notes\n[capture]\nmax_side = 1568  # small\n", encoding="utf-8")
    s = config.set_profile("work")
    assert s.work and s.max_side == 1568
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# my notes\n[capture]\nmax_side = 1568  # small\n") and '[profile]\nactive = "work"' in text
    path.write_text('[profile] # which one\n# active = "personal"\nactive = "personal" # set by me\n', encoding="utf-8")
    config.set_profile("work")
    assert path.read_text(encoding="utf-8") == '[profile] # which one\n# active = "personal"\nactive = "work" # set by me\n'


def test_set_profile_refuses_bad_names_and_broken_files(argus_home):
    with pytest.raises(ValueError):
        config.set_profile("home")
    argus_home.mkdir(parents=True, exist_ok=True)
    (argus_home / "config.toml").write_text("[profile\nactive = 'personal'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="valid TOML"):
        config.set_profile("work")


def test_cli_profile_command(argus_home, capsys):
    from argus.cli import main

    assert main(["profile"]) == 0
    assert "Active profile: personal" in capsys.readouterr().out
    assert main(["profile", "work"]) == 0
    out = capsys.readouterr().out
    assert "Switched to the work profile" in out and "Nothing is allowed yet" in out
    assert config.settings().work


def test_set_work_list_preserves_comments_crlf_and_multiline(argus_home):
    path = argus_home / "config.toml"
    argus_home.mkdir(parents=True, exist_ok=True)
    original = '# note\r\n[work] # section\r\nallow_apps = [\r\n  "Code.exe", # keep this\r\n]\r\nallow_titles = []\r\n'
    path.write_bytes(original.encode())
    config.set_work_list("allow_apps", ["Code.exe", "Terminal.EXE", "terminal.exe"])
    changed = path.read_bytes().decode()
    assert "# note\r\n[work] # section\r\n" in changed and "# keep this" in changed
    assert "\r\n" in changed and config.settings().allow_apps == {"code.exe", "terminal.exe"}


def test_set_work_list_creates_missing_section_and_key(argus_home):
    path = argus_home / "config.toml"
    argus_home.mkdir(parents=True, exist_ok=True)
    path.write_text("# note\n[capture]\nmax_side = 900\n", encoding="utf-8")
    config.set_work_list("allow_apps", ["Code.exe"])
    assert config.settings().allow_apps == {"code.exe"}
    assert "[work]\nallow_apps = [\"Code.exe\"]" in path.read_text(encoding="utf-8")


def test_set_work_list_rejects_invalid_values(argus_home):
    with pytest.raises(ValueError, match=r"\.exe"):
        config.set_work_list("allow_apps", ["Code"])
    with pytest.raises(ValueError, match="regex"):
        config.set_work_list("allow_titles", ["("])


def test_allow_learn_never_prints_window_titles(argus_home, monkeypatch, capsys):
    from types import SimpleNamespace
    from argus import cli
    from conftest import desk, mon, win

    windows = [win(1, "Doe, John (MRN 12345)", app="Editor", process="Editor.exe"),
               win(2, "SECRET TAB", app="Browser", process="browser.exe")]
    fake_desk = desk([mon(1, 0, 0, 800, 600)], windows)
    monkeypatch.setattr("argus.desktop.snapshot", lambda *_: fake_desk)
    tracker = SimpleNamespace(recent=lambda: [], stop=lambda: None)
    monkeypatch.setattr("argus.service.Tracker", lambda: SimpleNamespace(start=lambda: tracker))
    answers = iter(["2", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert cli.main(["allow", "--learn"]) == 0
    output = capsys.readouterr().out
    assert "Doe" not in output and "MRN" not in output and "SECRET TAB" not in output
    assert "Editor.exe" in output and config.settings().allow_apps == {"editor.exe"}


def test_set_work_list_hash_inside_string_and_comments(tmp_path, monkeypatch):
    from argus import config
    monkeypatch.setenv("ARGUS_HOME", str(tmp_path))
    config._cache = None
    (tmp_path / "config.toml").write_text(
        '[work]\nallow_titles = [\n  "C# notes",  # keep me\n]\nallow_apps = ["Code.exe"]\n', encoding="utf-8")
    s = config.set_work_list("allow_titles", ["C# notes", "x"])
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "# keep me" in text and 'pattern"]' not in text
    assert [p.pattern for p in s.allow_titles] == ["C# notes", "x"]
    assert s.allow_apps == frozenset({"code.exe"})
