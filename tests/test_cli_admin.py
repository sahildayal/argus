"""`argus allow` / `audit` / `status` and the config.toml writers behind them."""

import json
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor

import pytest

from argus import config
from argus.cli import main


def _write(argus_home, text: str):
    argus_home.mkdir(parents=True, exist_ok=True)
    path = argus_home / "config.toml"
    path.write_text(text, encoding="utf-8")
    config._cache = None
    return path


def _raw(path, key="allow_apps"):
    return tomllib.loads(path.read_text(encoding="utf-8"))["work"][key]


# --------------------------------------------------------------- argus allow


def test_allow_add_and_remove_keep_the_users_spelling_and_order(argus_home, capsys):
    path = _write(argus_home, '# mine\n[work]\nallow_apps = ["Code.exe", "WindowsTerminal.EXE"]  # keep\n')
    assert main(["allow", "add", "Notepad.exe", "code.EXE"]) == 0
    assert _raw(path) == ["Code.exe", "WindowsTerminal.EXE", "Notepad.exe"]
    out = capsys.readouterr().out
    assert "code.EXE was already in the list." in out
    assert main(["allow", "remove", "windowsterminal.exe", "Nope.exe"]) == 0
    assert _raw(path) == ["Code.exe", "Notepad.exe"]
    assert "Nope.exe wasn't in the list." in capsys.readouterr().out
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# mine\n") and "# keep" in text


def test_allow_never_rebuilds_the_list_from_lowercased_settings(argus_home):
    path = _write(argus_home, '[work]\nallow_browser_titles = ["^Jira", "\\\\bLocalHost\\\\b"]\n')
    assert main(["allow", "add-title", "^Confluence", "--browser"]) == 0
    assert _raw(path, "allow_browser_titles") == ["^Jira", "\\bLocalHost\\b", "^Confluence"]
    assert main(["allow", "remove-title", "^Jira", "--browser"]) == 0
    assert _raw(path, "allow_browser_titles") == ["\\bLocalHost\\b", "^Confluence"]


def test_allow_refuses_to_edit_while_config_has_problems(argus_home, capsys):
    config.set_profile("work")
    path = _write(argus_home, '[profile]\nactive = "work"\n[work]\nallow_apps = ["Code.exe"]\nallow_titles = ["("]\n')
    before = path.read_bytes()
    assert config.settings().lockdown
    assert main(["allow", "add", "Notepad.exe"]) == 2
    err = capsys.readouterr().err
    assert "Config problem: work.allow_titles" in err and "won't edit it" in err
    assert path.read_bytes() == before
    with pytest.raises(config.ConfigProblems):
        config.edit_work_list("allow_apps", add=["Notepad.exe"])
    assert path.read_bytes() == before


def test_allow_list_shows_problems_and_the_raw_lists_in_lockdown(argus_home, capsys):
    config.set_profile("work")
    _write(argus_home, '[profile]\nactive = "work"\n[work]\nallow_apps = ["Code.exe"]\nallow_titles = ["("]\n')
    assert main(["allow"]) == 0
    out = capsys.readouterr().out
    assert "Config problem: work.allow_titles" in out
    assert "Allowed apps: Code.exe" in out and "Allowed title patterns: (" in out
    assert "NOT in effect" in out


def test_allow_list_in_personal_says_lists_only_apply_in_work(argus_home, capsys):
    _write(argus_home, '[work]\nallow_apps = ["Code.exe"]\n')
    assert main(["allow"]) == 0
    out = capsys.readouterr().out
    assert "Allowed apps: Code.exe" in out and "only in the work profile" in out


def test_allow_reports_writer_errors_clearly(argus_home, capsys):
    _write(argus_home, "[work]\nallow_titles = []\n")
    assert main(["allow", "add-title", "("]) == 2
    assert "invalid regex" in capsys.readouterr().err
    assert main(["allow", "add", "Code"]) == 2
    assert ".exe" in capsys.readouterr().err
    assert main(["allow", "add", "Code.exe", "--browser"]) == 2
    assert "--browser" in capsys.readouterr().err


def test_concurrent_delta_edits_never_resurrect_removed_entries(argus_home):
    path = _write(argus_home, '[work]\nallow_apps = ["Old.exe"]\n')
    adds = [f"App{i}.exe" for i in range(8)]
    start = threading.Barrier(len(adds) + 1)

    def add(name):
        start.wait()
        config.edit_work_list("allow_apps", add=[name])

    def remove():
        start.wait()
        config.edit_work_list("allow_apps", remove=["old.exe"])

    with ThreadPoolExecutor(max_workers=len(adds) + 1) as pool:
        jobs = [pool.submit(add, name) for name in adds] + [pool.submit(remove)]
        for job in jobs:
            job.result(30)
    final = _raw(path)
    assert "Old.exe" not in final and sorted(final) == sorted(adds)


def test_edit_detects_a_file_saved_by_another_program_meanwhile(argus_home, monkeypatch):
    path = _write(argus_home, '[work]\nallow_apps = ["Code.exe"]\n')
    original = config._with_work_list
    external = '[work]\nallow_apps = ["Saved.exe"]\n'

    def racing(text, key, values):
        path.write_text(external, encoding="utf-8")  # e.g. an editor saving without the lock
        return original(text, key, values)

    monkeypatch.setattr(config, "_with_work_list", racing)
    with pytest.raises(ValueError, match="changed while"):
        config.edit_work_list("allow_apps", add=["Notepad.exe"])
    assert path.read_text(encoding="utf-8") == external
    assert not list(argus_home.glob("config.toml.*.tmp"))


def test_edit_waits_for_the_config_lock_then_gives_up(argus_home, monkeypatch):
    path = _write(argus_home, '[work]\nallow_apps = []\n')
    monkeypatch.setattr(config, "_LOCK_TIMEOUT", 0.2)
    with config._ConfigLock(path):
        with pytest.raises(ValueError, match="another Argus process"):
            config.edit_work_list("allow_apps", add=["Code.exe"])
    assert config.edit_work_list("allow_apps", add=["Code.exe"]) == ([], ["Code.exe"])


def test_set_profile_and_work_lists_share_the_verified_writer(argus_home):
    path = _write(argus_home, '[profile]\nactive = "personal"\nnote = """\n[work]\nallow_apps = []\n"""\n')
    # The [work] header sits inside a multi-line string, so the edit can't be verified.
    with pytest.raises(ValueError, match="couldn't safely update"):
        config.set_work_list("allow_apps", ["Code.exe"])
    assert "Code.exe" not in path.read_text(encoding="utf-8")


def test_profile_hint_points_to_learn_mode(argus_home, capsys):
    assert main(["profile", "work"]) == 0
    assert "argus allow --learn" in capsys.readouterr().out


# --------------------------------------------------------------- argus audit


def _audit_lines(argus_home, *stamps):
    folder = argus_home / "audit"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": ts, "tool": f"tool{i}", "decision": "sent", "reason": "user", "apps": []})
             for i, ts in enumerate(stamps)]
    (folder / "audit-2026-01.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_audit_since_must_be_a_date(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["audit", "--since", "last tuesday"])
    assert exc.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["audit", "-n", "-3"])


def test_audit_count_applies_with_since(argus_home, capsys):
    _audit_lines(argus_home, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z",
                 "2026-01-04T00:00:00Z")
    assert main(["audit", "--since", "2026-01-02", "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert "tool2" in out and "tool3" in out and "tool1" not in out and "tool0" not in out
    assert "UTC" in out
    assert main(["audit", "--since", "2026-01-02", "-n", "0"]) == 0
    out = capsys.readouterr().out
    assert all(f"tool{i}" in out for i in (1, 2, 3)) and "tool0" not in out


def test_audit_export_keeps_since_given_before_the_subcommand(argus_home, tmp_path, capsys):
    _audit_lines(argus_home, "2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z")
    out = tmp_path / "export.jsonl"
    assert main(["audit", "--since", "2026-01-03", "export", "-o", str(out)]) == 0
    assert [json.loads(line)["tool"] for line in out.read_text().splitlines()] == ["tool1"]
    assert main(["audit", "export", "-o", str(out), "--since", "2026-01-01"]) == 0
    assert len(out.read_text().splitlines()) == 2


@pytest.mark.parametrize("message, hint", [
    ("Audit log tail truncated: head.json records 5 entries, found 3.", "accidental loss"),
    ("Invalid audit pruning anchor month for the retained files.", "anchor.json"),
    ("Chain broken at audit-2026-01.jsonl:2 (expected prev abc).", "every later entry"),
])
def test_audit_verify_explains_each_problem_type(monkeypatch, capsys, message, hint):
    from argus import audit

    monkeypatch.setattr(audit, "verify_chain", lambda: (False, message))
    assert main(["audit", "verify"]) == 1
    err = capsys.readouterr().err
    assert message in err and "What this means:" in err and hint in err


# -------------------------------------------------------------- argus status


def _diagnose(monkeypatch, *, running, pipe):
    from argus import gate_pipe, install

    monkeypatch.setattr(install, "tray_running", lambda: running)
    monkeypatch.setattr(gate_pipe, "probe", lambda *a, **k: pipe)


def test_status_in_work_explains_an_unreachable_tray(monkeypatch, capsys):
    _diagnose(monkeypatch, running=True, pipe="missing")
    config.set_profile("work")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Approval gate: on" in out and "Audit log: on" in out
    assert "Tray process running: yes; approval pipe: not found" in out
    assert "UNAVAILABLE" in out and "if you upgraded Argus, quit the old tray" in out
    assert "Config problems: none" in out


def test_status_in_personal_with_a_verified_tray(monkeypatch, capsys):
    _diagnose(monkeypatch, running=True, pipe="verified")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Approval gate: off" in out and "Audit log: off" in out
    assert "reachable and verified" in out and "UNAVAILABLE" not in out


def test_status_shows_personal_opt_ins_and_config_problems(argus_home, monkeypatch, capsys):
    _diagnose(monkeypatch, running=False, pipe="missing")
    _write(argus_home, "[gate]\nenabled = true\n[audit]\nenabled = true\n[capture]\ngrid = 1\n")
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Approval gate: on ([gate] enabled = true)" in out and "Audit log: on ([audit] enabled = true)" in out
    assert "UNAVAILABLE" in out and "Config problems: 1" in out and "capture.grid" in out


def test_doctor_reports_profile_gate_audit_tray_and_problems(argus_home, monkeypatch, capsys):
    from argus import install
    from conftest import desk, mon

    _diagnose(monkeypatch, running=False, pipe="missing")
    monkeypatch.setattr(install, "CLIENTS", [])
    monkeypatch.setattr("argus.desktop.snapshot", lambda *_a, **_k: desk([mon(1, 0, 0, 800, 600)], []))
    config.set_profile("work")
    _write(argus_home, '[profile]\nactive = "work"\n[gate]\nenabled = "yes"\n')
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Profile: work" in out and "Approval gate: on" in out and "Audit log: on" in out
    assert "Tray process running: no; approval pipe: not found" in out and "UNAVAILABLE" in out
    assert "Config problem: gate.enabled" in out
