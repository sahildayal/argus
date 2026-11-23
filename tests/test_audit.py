"""Tests for the append-only, tamper-evident gate audit log."""

from __future__ import annotations

import concurrent.futures
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
    import hashlib

    assert record["image_sha256"] == [hashlib.sha256(shot_bytes).hexdigest()]
    assert record["text_chars"] == len(caption_text)
    assert record["request_id"] == decision.request_id
    assert record["client"] == "test-client-suite"
    assert record["argus_version"] == __version__
    assert record["prev"] == "genesis"
    assert record["ts"].endswith("Z")


def test_audit_never_writes_sensitive_data(tmp_path):
    """Assert that recognizable secret strings (window titles, OCR text, pixels) never appear in the audit file."""
    set_profile("work")
    secret_mrn = "MRN 123456"
    secret_patient = "Doe, John"
    secret_pixel_data = b"secret-pixel-data-with-MRN 123456"
    secret_text = f"Doctor notes for patient {secret_patient} with ID {secret_mrn}"
    secret_title = f"{secret_patient} ({secret_mrn}) - Electronic Health Record"

    def transport(preview):
        return "user"

    gate.request(
        "read_text",
        (secret_pixel_data,),
        (secret_text,),
        windows=WINDOWS,
        transport=transport,
    )

    audit_files = list((home() / "audit").glob("audit-*.jsonl"))
    assert len(audit_files) == 1
    raw_content = audit_files[0].read_text(encoding="utf-8")

    assert secret_mrn not in raw_content
    assert secret_patient not in raw_content
    assert "123456" not in raw_content
    assert "Doctor notes" not in raw_content
    assert secret_title not in raw_content
    assert "secret-pixel-data" not in raw_content


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


def test_audit_multi_month_chaining(tmp_path):
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
    import hashlib
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

    # 13 months ago
    m13_year = now_utc.year - 1 if now_utc.month > 1 else now_utc.year - 2
    m13_month = now_utc.month - 1 if now_utc.month > 1 else 12
    # Adjust for exactly 13 months ago
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

