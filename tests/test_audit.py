"""Tests for the append-only, tamper-evident gate audit log."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from argus import __version__, audit, gate, privacy
from argus.config import config_path, home, set_profile, settings
from argus.gate import Decision


WINDOWS = frozenset({(1, "code.exe", 101), (2, "browser.exe", 202)})


def _clock(monkeypatch, *dates):
    times = iter(datetime.fromisoformat(date).replace(tzinfo=timezone.utc) for date in dates)

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return next(times).astimezone(tz)

    monkeypatch.setattr(audit, "datetime", Clock)


def _decision(when=0):
    return Decision(True, "user", "screenshot", ["code.exe"], when, "request")


def test_audit_fields_and_format(tmp_path):
    set_profile("work")
    shot_bytes = b"fake-png-bytes-for-test"
    caption_text = "Window screenshot caption"

    def transport(preview):
        return "user"

    decision = gate.request(
        "screenshot",
        (shot_bytes,),
        (caption_text,),
        windows=WINDOWS,
        transport=transport,
        client="test-client-suite",
    )
    assert decision.approved is True
    assert decision.reason == "user"

    audit_files = list((home() / "audit").glob("audit-*.jsonl"))
    assert len(audit_files) == 1

    lines = audit_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["profile"] == "work"
    assert record["tool"] == "screenshot"
    assert record["decision"] == "sent"
    assert record["reason"] == "user"
    assert record["apps"] == ["browser.exe", "code.exe"]
    assert record["images"] == 1
    assert record["image_sha256"] == [hashlib.sha256(shot_bytes).hexdigest()]
    assert record["text_chars"] == len(caption_text)
    assert record["request_id"] == decision.request_id
    assert record["client"] == "other:" + hashlib.sha256(b"test-client-suite").hexdigest()[:8]
    assert record["argus_version"] == __version__
    assert record["prev"] == "genesis"
    assert record["ts"].endswith("Z")


def test_audit_never_writes_sensitive_data(tmp_path, monkeypatch):
    from mcp import Client
    from mcp_types import Implementation
    from PIL import Image

    from argus import server, service
    from argus.capture import Shot
    from argus.desktop import NO_HOST, ArgusError

    set_profile("work")
    title, ocr_text, error_text = "ZQXSECRETTITLE", "ZQXSECRETOCR", "ZQXSECRETERR"
    path = tmp_path / "ZQXSECRETCAPTURE.png"
    shot = Shot(
        Image.new("RGB", (40, 32), "navy"), "window", title, (0, 0), {"window": 1},
        profile="work", share_windows=WINDOWS, policy=settings().policy,
    )
    session = service.Session(host=NO_HOST, track=False)
    monkeypatch.setattr(server, "_session", session)
    monkeypatch.setattr(session, "desk", lambda: None)

    def capture(_desk, _settings, monitor, *_args):
        if monitor == "path":
            raise ArgusError(f"{error_text}: {path}")
        return [shot]

    async def read(_image):
        return [service.ocr.Line(ocr_text, (service.ocr.Word(ocr_text, (0, 0, 20, 8)),))]

    seen = []
    monkeypatch.setattr(service, "capture", capture)
    monkeypatch.setattr(service.ocr, "read", read)
    monkeypatch.setattr(gate, "_transport", lambda preview: seen.append(preview) or "user")

    async def call_tools():
        async with Client(server.mcp, client_info=Implementation(name="audit-test\nclient", version="1")) as client:
            shot_result = await client.call_tool("screenshot", {})
            ocr_result = await client.call_tool("read_text", {})
            error_result = await client.call_tool("screenshot", {"monitor": "path"})
            return shot_result, ocr_result, error_result

    shot_result, ocr_result, error_result = asyncio.run(call_tools())
    session.store.flush()
    assert not shot_result.is_error and title in shot_result.content[0].text
    assert not ocr_result.is_error and ocr_text in ocr_result.content[0].text
    assert error_result.is_error and error_text in error_result.content[0].text
    assert str(path) in seen[2].text[0]

    (log,) = (home() / "audit").glob("audit-*.jsonl")
    raw = log.read_text(encoding="utf-8")
    image = next(c.data for c in shot_result.content if c.type == "image")
    escaped_tmp = json.dumps(str(tmp_path))[1:-1]
    for secret in (title, ocr_text, error_text, path.name, tmp_path.as_posix(), escaped_tmp, image):
        assert secret not in raw
    entries = audit.read_entries(limit=None)
    assert [e["tool"] for e in entries] == ["screenshot", "read_text", "screenshot"]
    assert [e["request_id"] for e in entries] == [p.request_id for p in seen]
    expected_client = "other:" + hashlib.sha256(b"audit-test\nclient").hexdigest()[:8]
    assert all(e["profile"] == "work" and e["client"] == expected_client for e in entries)
    assert [e["images"] for e in entries] == [1, 0, 0]
    assert entries[0]["image_sha256"] == [hashlib.sha256(base64.b64decode(image)).hexdigest()]
    assert gate.request("other", required=False).approved
    assert audit.read_entries()[-1]["client"] is None


def test_audit_hash_chain_tamper_evidence():
    set_profile("work")

    def transport(preview):
        return "user"

    for i in range(5):
        gate.request("look", (), (f"lead text {i}",), transport=transport)

    valid, msg = audit.verify_chain()
    assert valid is True
    assert "Verified 5 entries" in msg
    assert "chain valid" in msg

    # Tamper with the third line
    audit_files = sorted((home() / "audit").glob("audit-*.jsonl"))
    lines = audit_files[0].read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[2])
    data["tool"] = "tampered_tool"
    lines[2] = json.dumps(data)
    audit_files[0].write_text("\n".join(lines) + "\n", encoding="utf-8")

    valid, msg = audit.verify_chain()
    assert valid is False
    assert "Chain broken" in msg
    assert "line 4" in msg or ":4" in msg  # Line 4's prev doesn't match line 3's new hash


def test_audit_multi_month_chaining(tmp_path, monkeypatch):
    _clock(monkeypatch, "2026-10-15", "2026-10-20", "2026-11-01")
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    # Month 1: 2026-10
    d1 = Decision(True, "user", "shot", ["code.exe"], datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).timestamp(), "r1")
    d2 = Decision(True, "user", "look", ["code.exe"], datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc).timestamp(), "r2")
    audit.log_decision(d1, dir_path=audit_dir)
    audit.log_decision(d2, dir_path=audit_dir)

    file_oct = audit_dir / "audit-2026-10.jsonl"
    assert file_oct.exists()
    oct_lines = file_oct.read_text(encoding="utf-8").strip().splitlines()
    assert len(oct_lines) == 2
    rec1 = json.loads(oct_lines[0])
    rec2 = json.loads(oct_lines[1])
    assert rec1["prev"] == "genesis"
    assert rec2["prev"] == hashlib.sha256(oct_lines[0].encode("utf-8")).hexdigest()

    # Month 2: 2026-11
    d3 = Decision(True, "user", "ocr", ["code.exe"], datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc).timestamp(), "r3")
    audit.log_decision(d3, dir_path=audit_dir)

    file_nov = audit_dir / "audit-2026-11.jsonl"
    assert file_nov.exists()
    nov_lines = file_nov.read_text(encoding="utf-8").strip().splitlines()
    assert len(nov_lines) == 1
    rec3 = json.loads(nov_lines[0])
    # rec3's prev must chain to the last line of October file!
    assert rec3["prev"] == hashlib.sha256(oct_lines[-1].encode("utf-8")).hexdigest()

    valid, msg = audit.verify_chain(audit_dir)
    assert valid is True
    assert "Verified 3 entries across 2 file(s): chain valid." == msg


def test_audit_fail_closed():
    set_profile("work")

    def transport(preview):
        return "user"

    # Simulate an audit write failure (e.g. disk full, permission denied)
    with patch("argus.audit.log_decision", side_effect=OSError("Disk full")):
        decision = gate.request("screenshot", (b"bytes",), windows=WINDOWS, transport=transport)
        assert decision.approved is False
        assert decision.reason == "audit_failed"

        with pytest.raises(gate.GateStopped) as exc_info:
            gate.require(decision)
        assert str(exc_info.value) == "The audit log could not be written."


def test_audit_fail_closed_in_mcp_server():
    from mcp.server.mcpserver.exceptions import ToolError
    from argus import server

    set_profile("work")

    with patch("argus.audit.log_decision", side_effect=OSError("Disk full")):
        with pytest.raises(ToolError) as exc_info:
            server.screenshot()
        assert "The audit log could not be written." in str(exc_info.value)


def test_audit_config_defaults():
    # In personal profile: default audit is False
    set_profile("personal")
    s = settings()
    assert s.profile == "personal"
    assert s.audit_enabled is False
    assert s.requires_audit is False

    # In work profile: auditing is always required
    set_profile("work")
    s = settings()
    assert s.profile == "work"
    assert s.requires_audit is True

    # Opt-in in personal (work is sticky: switch back explicitly first)
    set_profile("personal")
    config_path().write_text('[profile]\nactive = "personal"\n[audit]\nenabled = true\n', encoding="utf-8")
    s = settings()
    assert s.profile == "personal"
    assert s.audit_enabled is True
    assert s.requires_audit is True

    # The work profile can't opt out
    config_path().write_text('[profile]\nactive = "work"\n[audit]\nenabled = false\n', encoding="utf-8")
    s = settings()
    assert s.profile == "work"
    assert s.requires_audit is True


def test_audit_retention_and_purge(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy files for several months
    now_utc = datetime.now(timezone.utc)
    current_file = audit_dir / f"audit-{now_utc.year:04d}-{now_utc.month:02d}.jsonl"
    current_file.write_text("current\n", encoding="utf-8")

    # 5 months ago
    m5_year = now_utc.year if now_utc.month > 5 else now_utc.year - 1
    m5_month = now_utc.month - 5 if now_utc.month > 5 else now_utc.month + 7
    m5_file = audit_dir / f"audit-{m5_year:04d}-{m5_month:02d}.jsonl"
    m5_file.write_text("m5\n", encoding="utf-8")

    boundary_file = audit_dir / f"audit-{now_utc.year - 1:04d}-{now_utc.month:02d}.jsonl"
    boundary_file.write_text("boundary\n", encoding="utf-8")

    # 13 months ago
    m13_year = now_utc.year - 1
    m13_month = now_utc.month - 1
    if m13_month <= 0:
        m13_month += 12
        m13_year -= 1
    m13_file = audit_dir / f"audit-{m13_year:04d}-{m13_month:02d}.jsonl"
    m13_file.write_text("m13\n", encoding="utf-8")

    removed = audit.cleanup_audit(retention_months=12, dir_path=audit_dir)
    assert removed == 1
    assert not m13_file.exists()
    assert current_file.exists()
    assert m5_file.exists()
    assert boundary_file.exists()

    # Test that privacy.purge_all() does NOT delete audit files
    # Set home to tmp_path
    with patch("argus.privacy.home", return_value=tmp_path):
        captures = tmp_path / "captures"
        captures.mkdir(parents=True, exist_ok=True)
        (captures / "cap1.png").write_text("cap", encoding="utf-8")

        privacy.purge_all()
        assert not captures.exists()
        assert current_file.exists()
        assert m5_file.exists()


def test_audit_concurrency():
    set_profile("work")

    def log_one(idx: int):
        d = Decision(True, "user", f"tool_{idx}", ["app.exe"], time.time(), f"rid_{idx}")
        audit.log_decision(d, shots=(b"bytes",), text=(f"text {idx}",))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(log_one, i) for i in range(25)]
        concurrent.futures.wait(futures)
        for f in futures:
            f.result()

    valid, msg = audit.verify_chain()
    assert valid is True
    assert "Verified 25 entries" in msg


def test_audit_cli(capsys, tmp_path):
    from argus.cli import main

    set_profile("work")

    def transport(preview):
        return "user"

    gate.request("screenshot", (b"img1",), ("text1",), windows=WINDOWS, transport=transport)
    gate.request("look", (), ("text2",), windows=WINDOWS, transport=transport)

    # 1. argus audit
    ret = main(["audit"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Time" in out
    assert "screenshot" in out
    assert "look" in out
    assert "browser.exe" in out

    # 2. argus audit --since
    ret = main(["audit", "--since", "2020-01-01"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "screenshot" in out

    # 3. argus audit verify
    ret = main(["audit", "verify"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "chain valid" in out

    # 4. argus audit export --out file.jsonl
    export_file = tmp_path / "exported.jsonl"
    ret = main(["audit", "export", "--out", str(export_file)])
    assert ret == 0
    assert export_file.exists()
    lines = export_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_audit_empty_cli(capsys):
    from argus.cli import main

    set_profile("personal")
    # No logs exist
    ret = main(["audit"])
    assert ret == 0
    assert "No audit log entries found." in capsys.readouterr().out

    ret = main(["audit", "verify"])
    assert ret == 0
    assert "No audit logs found." in capsys.readouterr().out


def test_audit_all_reasons_logged_when_gate_enabled():
    set_profile("work")

    # 1. user approval
    gate.request("shot", windows=WINDOWS, transport=lambda p: "user")
    # 2. denied
    gate.request("shot", windows=WINDOWS, transport=lambda p: "denied")
    # 3. timeout
    gate.request("shot", windows=WINDOWS, transport=lambda p: (_ for _ in ()).throw(TimeoutError()))
    # 4. tray_unavailable
    gate.request("shot", windows=WINDOWS, transport=lambda p: (_ for _ in ()).throw(FileNotFoundError()))
    # 5. not_required (e.g. required=False)
    gate.request("list_screens", required=False)

    entries = audit.read_entries(limit=None)
    assert len(entries) == 5

    reasons = [e["reason"] for e in entries]
    decisions = [e["decision"] for e in entries]

    assert reasons == ["user", "denied", "timeout", "tray_unavailable", "not_required"]
    assert decisions == ["sent", "denied", "denied", "denied", "sent"]


def test_audit_verify_broken_scenarios(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    # 1. File with non-genesis first line
    bad_first = audit_dir / "audit-2026-01.jsonl"
    bad_first.write_text(json.dumps({"ts": "2026-01-01T00:00:00Z", "prev": "invalid_hash"}) + "\n", encoding="utf-8")
    valid, msg = audit.verify_chain(audit_dir)
    assert valid is False
    assert "expected genesis prev" in msg

    # 2. File with invalid JSON
    bad_first.write_text("not json!\n", encoding="utf-8")
    valid, msg = audit.verify_chain(audit_dir)
    assert valid is False
    assert "invalid JSON" in msg


def test_pruning_anchors_the_retained_chain(monkeypatch, capsys):
    from argus.cli import main

    _clock(monkeypatch, "2025-10-01", "2025-11-01", "2026-11-01")
    audit.log_decision(_decision())
    audit.log_decision(_decision())
    base = home() / "audit"
    discarded = base / "audit-2025-10.jsonl"
    expected = hashlib.sha256(discarded.read_bytes().strip()).hexdigest()

    assert audit.cleanup_audit(retention_months=12) == 1
    assert not discarded.exists()
    anchor_path = base / "anchor.json"
    anchor = json.loads(anchor_path.read_bytes())
    assert anchor == {
        "prev_of_first_kept": expected, "pruned_through": "2025-10", "ts": "2026-11-01T00:00:00Z",
    }
    assert main(["audit", "verify"]) == 0
    out = capsys.readouterr().out
    assert "chain valid" in out and "pruned through 2025-10" in out

    anchor["prev_of_first_kept"] = "0" * 64
    anchor_path.write_text(json.dumps(anchor), encoding="utf-8")
    assert main(["audit", "verify"]) == 1
    assert "Chain broken" in capsys.readouterr().err


def test_writing_after_all_months_are_pruned(monkeypatch):
    _clock(monkeypatch, "2025-12-01", "2026-02-01", "2025-12-02")
    audit.log_decision(_decision())
    assert audit.cleanup_audit(retention_months=1) == 1
    assert "pruned through 2025-12" in audit.verify_chain()[1]
    audit.log_decision(_decision())
    assert (home() / "audit" / "audit-2026-01.jsonl").exists()
    assert audit.verify_chain()[0]


def test_verify_recovers_an_interrupted_prune(monkeypatch):
    _clock(monkeypatch, "2025-10-01", "2025-11-01", "2026-11-01")
    audit.log_decision(_decision())
    audit.log_decision(_decision())
    replace = Path.replace

    def interrupt_commit(path, target):
        if target.name == "anchor.json":
            raise OSError("interrupted prune")
        return replace(path, target)

    with monkeypatch.context() as m:
        m.setattr(Path, "replace", interrupt_commit)
        with pytest.raises(OSError, match="interrupted prune"):
            audit.cleanup_audit(retention_months=12)

    base = home() / "audit"
    assert not (base / "audit-2025-10.jsonl").exists()
    valid, msg = audit.verify_chain()
    assert valid and "pruned through 2025-10" in msg
    assert (base / "anchor.json").exists()
    assert not (base / "anchor.json.pending").exists()


def test_rollover_uses_locked_clock_instead_of_decision_time(monkeypatch):
    _clock(monkeypatch, "2026-10-31T23:59:59", "2026-11-01T00:00:01", "2026-10-31T23:59:58")
    early = datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()
    late = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    audit.log_decision(_decision(early))
    first_writer = audit.log_decision(_decision(late))
    delayed_writer = audit.log_decision(_decision(early))
    assert first_writer["ts"] == "2026-11-01T00:00:01Z"
    assert delayed_writer["ts"] == "2026-10-31T23:59:58Z"
    base = home() / "audit"
    assert len((base / "audit-2026-10.jsonl").read_bytes().splitlines()) == 1
    assert len((base / "audit-2026-11.jsonl").read_bytes().splitlines()) == 2
    assert audit.verify_chain() == (True, "Verified 3 entries across 2 file(s): chain valid.")


@pytest.mark.parametrize("next_date", ["2026-11-02", "2026-12-01"])
def test_torn_invalid_utf8_line_recovers_across_appends(monkeypatch, next_date):
    _clock(monkeypatch, "2026-11-01", next_date, next_date)
    audit.log_decision(_decision())
    path = home() / "audit" / "audit-2026-11.jsonl"
    intact = path.read_bytes()
    fragment = b'{"tool":"' + b"x" * 4096 + b"\xff"
    path.write_bytes(intact + fragment)
    valid, msg = audit.verify_chain()
    assert valid and "incomplete line 2 (crash?)" in msg

    recovered = audit.log_decision(_decision())
    assert recovered["recovered"] is True
    assert recovered["prev"] == hashlib.sha256(intact.strip()).hexdigest()
    assert "recovered" not in audit.log_decision(_decision())
    if next_date.startswith("2026-11"):
        assert path.read_bytes().startswith(intact + fragment + b"\n{")
    valid, msg = audit.verify_chain()
    assert valid and "Verified 3 entries" in msg and "incomplete line 2 (crash?)" in msg
    assert len(audit.read_entries()) == 3


def test_audit_append_writes_once_then_syncs(monkeypatch):
    writes = []
    write = audit.os.write
    fsync = audit.os.fsync

    def track_write(fd, data):
        writes.append(data)
        return write(fd, data)

    def track_sync(fd):
        writes.append("fsync")
        return fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(audit.os, "write", track_write)
        m.setattr(audit.os, "fsync", track_sync)
        audit.log_decision(_decision())
    line, line_sync, head_sync = writes
    assert line_sync == head_sync == "fsync" and line.endswith(b"\n") and b"\r" not in line
    assert json.loads(line)["prev"] == "genesis"


@pytest.mark.parametrize(("name", "product"), [
    ("CLAUDE-Code / private workspace", "claude-code"),
    ("Visual Studio CODE: sensitive title", "vscode"),
    ("Claude Desktop internal", "claude-desktop"),
    ("é\n\ud800\u202e\u200b\ufe0f\u034f" + "x" * 1000, None),
])
def test_client_name_never_copies_self_reported_text(name, product):
    audit.log_decision(_decision(), client=name)
    (path,) = (home() / "audit").glob("audit-*.jsonl")
    raw = path.read_bytes()
    assert raw.isascii() and raw.count(b"\n") == 1
    expected = product or "other:" + hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    assert json.loads(raw)["client"] == expected


def test_audit_fails_closed_without_os_lock(monkeypatch):
    set_profile("work")
    monkeypatch.setattr(audit, "_HAS_MSVCRT", False)
    monkeypatch.setattr(audit, "_HAS_FCNTL", False)
    decision = gate.request("screenshot", transport=lambda _preview: "user")
    assert not decision.approved and decision.reason == "audit_failed"
    assert not list((home() / "audit").glob("*.jsonl"))


def test_audit_uses_start_profile_after_policy_change():
    set_profile("work")
    start = gate.policy()

    def approve(_preview):
        set_profile("personal")
        return "user"

    decision = gate.request("screenshot", (b"img",), start=start, transport=approve)
    assert not decision.approved and decision.reason == "settings_changed"
    (entry,) = audit.read_entries()
    assert entry["profile"] == "work" and entry["reason"] == "settings_changed"


@pytest.mark.parametrize("change", ["pause", "marker"])
def test_audit_revalidates_approval_after_writing(monkeypatch, change):
    from argus import config

    set_profile("work")
    log_decision = audit.log_decision

    def log_then_change(decision, **kwargs):
        record = log_decision(decision, **kwargs)
        if decision.approved:
            if change == "pause":
                privacy.pause()
            else:
                config.write_profile_state("personal")
        return record

    monkeypatch.setattr(audit, "log_decision", log_then_change)
    decision = gate.request("look", (b"img",), transport=lambda _: "user")
    assert not decision.approved and decision.reason == "settings_changed"
    with pytest.raises(gate.GateStopped, match="Argus settings changed during the call"):
        gate.require(decision)
    first, second = audit.read_entries()
    assert first["request_id"] == second["request_id"] == decision.request_id
    assert (first["decision"], first["reason"], first["images"]) == ("sent", "user", 1)
    assert (second["decision"], second["reason"], second["images"]) == ("denied", "settings_changed", 0)
    assert audit.verify_chain()[0]


@pytest.mark.parametrize("remove_whole_file", [False, True])
def test_head_detects_loss_of_whole_records(remove_whole_file):
    audit.log_decision(_decision())
    audit.log_decision(_decision())
    base = home() / "audit"
    (path,) = base.glob("audit-*.jsonl")
    first, last = path.read_bytes().splitlines()
    head = json.loads((base / "head.json").read_bytes())
    assert head == {"hash": hashlib.sha256(last).hexdigest(), "count": 2, "ts": json.loads(last)["ts"]}
    if remove_whole_file:
        path.unlink()
    else:
        path.write_bytes(first + b"\n")
    assert audit.verify_chain() == (False, "Audit tail missing/truncated.")
    # Logging continues (captures aren't blocked forever) and the gap stays visible.
    assert audit.log_decision(_decision())["tail_missing"] is True
    ok, message = audit.verify_chain()
    assert not ok and "tail missing/truncated" in message


def test_append_recovers_when_head_replace_was_interrupted(monkeypatch):
    audit.log_decision(_decision())

    def crash(*_args):
        raise OSError("interrupted checkpoint write")

    with monkeypatch.context() as patch:
        patch.setattr(audit, "_write_head", crash)
        with pytest.raises(OSError, match="interrupted checkpoint"):
            audit.log_decision(_decision())
    assert not audit.verify_chain()[0]
    audit.log_decision(_decision())
    assert audit.verify_chain()[0]
    assert json.loads((home() / "audit" / "head.json").read_bytes())["count"] == 3


def test_export_protects_audit_files_and_reports_unparseable_lines(tmp_path, capsys):
    from argus.cli import main

    audit.log_decision(_decision())
    (path,) = (home() / "audit").glob("audit-*.jsonl")
    original = path.read_bytes()
    assert main(["audit", "export", "--out", str(path)]) == 1
    assert "outside the audit directory" in capsys.readouterr().err
    assert path.read_bytes() == original

    path.write_bytes(b"broken\n\xff\n\n" + original)
    output = tmp_path / "export.jsonl"
    assert main(["audit", "export", "--since", "2020-01-01", "--out", str(output)]) == 0
    assert "Skipped 2 unparseable audit line(s)" in capsys.readouterr().out
    assert output.read_bytes() == original


def test_images_and_hashes_only_include_sent_bytes():
    set_profile("work")
    shots = (b"valid", "unsupported")
    gate.request("screenshot", shots, transport=lambda _preview: "denied")
    gate.request("screenshot", shots, transport=lambda _preview: "user")
    gate.request("wait_for", (b"preview",), watching=True, transport=lambda _preview: "user")
    denied, sent, intermediate = audit.read_entries()
    assert denied["images"] == intermediate["images"] == 0
    assert denied["image_sha256"] == intermediate["image_sha256"] == []
    assert sent["images"] == 1 and sent["image_sha256"] == [hashlib.sha256(b"valid").hexdigest()]



def test_lost_head_checkpoint_keeps_logging_and_verify_reports_the_gap(tmp_path):
    from argus import audit
    from argus.gate import Decision

    def decide(i):
        return Decision(True, "user", "screenshot", ["Code.exe"], 0.0, f"{i:032x}")

    audit.log_decision(decide(1), dir_path=tmp_path)
    audit.log_decision(decide(2), dir_path=tmp_path)
    (tmp_path / "head.json").write_text('{"hash": "' + "0" * 64 + '", "count": 9, "ts": "2026-01-01T00:00:00Z"}\n')
    record = audit.log_decision(decide(3), dir_path=tmp_path)  # must not raise
    assert record["tail_missing"] is True
    ok, message = audit.verify_chain(tmp_path)
    assert not ok and "lost before" in message
