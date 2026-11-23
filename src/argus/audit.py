"""Append-only, tamper-evident audit log of gate disclosure decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import home

if TYPE_CHECKING:
    from .gate import Decision

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

_MONTH = r"[0-9]{4}-(?:0[1-9]|1[0-2])"
_MONTHLY_FILE = re.compile(rf"audit-({_MONTH})\.jsonl")
_ANCHOR_FILE = "anchor.json"
_PENDING_ANCHOR = "anchor.json.pending"
_HEAD_FILE = "head.json"
_TAIL_MISSING = "Audit tail missing/truncated."
_CLIENT_PREFIXES = {
    "claude-code": "claude-code",
    "claude-ai": "claude-ai",
    "claude desktop": "claude-desktop",
    "cursor": "cursor",
    "vscode": "vscode",
    "visual studio code": "vscode",
    "codex": "codex",
    "windsurf": "windsurf",
    "zed": "zed",
    "cline": "cline",
    "continue": "continue",
    "argus-cli": "argus-cli",
}


def audit_dir() -> Path:
    p = home() / "audit"
    p.mkdir(parents=True, exist_ok=True)
    return p


class AuditLock:
    """OS-level lock for concurrent audit writers (msvcrt on Windows, fcntl on Unix)."""

    def __init__(self, path: Path | None = None, timeout: float = 10.0):
        self.path = path or (audit_dir() / ".lock")
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self):
        if not (_HAS_MSVCRT or _HAS_FCNTL):
            raise RuntimeError("No OS audit lock is available.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        start = time.monotonic()
        while True:
            try:
                if _HAS_MSVCRT:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif _HAS_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return self
            except OSError:
                if time.monotonic() - start >= self.timeout:
                    os.close(fd)
                    raise TimeoutError(f"Could not acquire audit lock on {self.path} within {self.timeout}s")
                time.sleep(0.02)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                if _HAS_MSVCRT:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                elif _HAS_FCNTL:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None


def _monthly_files(base: Path) -> list[Path]:
    return sorted(p for p in base.glob("audit-*.jsonl") if _MONTHLY_FILE.fullmatch(p.name) and p.is_file())


def _parse_record(raw: bytes) -> dict | None:
    line = raw.decode("utf-8", errors="replace")
    if line.encode("utf-8") != raw:
        return None
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) else None


def _get_last_line(path: Path) -> tuple[bytes | None, bool]:
    """Find the last valid record, reading backward in blocks past any damaged tail."""
    recovered = False
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        carry = b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            lines = (f.read(step) + carry).split(b"\n")
            if pos:
                carry = lines.pop(0)
            for raw in reversed(lines):
                line = raw.strip()
                if line:
                    if _parse_record(line) is not None:
                        return line, recovered
                    recovered = True
    return None, recovered


def _load_anchor(path: Path) -> dict | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    anchor = _parse_record(raw)
    if anchor is not None:
        prev = anchor.get("prev_of_first_kept")
        month = anchor.get("pruned_through")
        ts = anchor.get("ts")
        if (isinstance(prev, str) and re.fullmatch(r"genesis|[0-9a-f]{64}", prev)
                and isinstance(month, str) and re.fullmatch(_MONTH, month)
                and isinstance(ts, str) and ts.endswith("Z")):
            return anchor
    raise ValueError("Invalid audit pruning anchor.")


def _read_anchor(base: Path) -> dict | None:
    """Resolve a prune interrupted between staging its anchor and committing it."""
    pending_path = base / _PENDING_ANCHOR
    pending = _load_anchor(pending_path)
    if pending:
        if (base / f"audit-{pending['pruned_through']}.jsonl").exists():
            pending_path.unlink()
        else:
            pending_path.replace(base / _ANCHOR_FILE)
    return _load_anchor(base / _ANCHOR_FILE)


def _find_prev_hash(files: list[Path], anchor: dict | None) -> tuple[str, bool]:
    recovered = False
    for path in reversed(files):
        last, damaged_tail = _get_last_line(path)
        recovered = recovered or damaged_tail
        if last is not None:
            return hashlib.sha256(last).hexdigest(), recovered
    return (anchor["prev_of_first_kept"] if anchor else "genesis"), recovered


def _utc_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_client(name: str | None) -> str | None:
    if name is None:
        return None
    normalized = name.strip().casefold()
    for prefix, product in _CLIENT_PREFIXES.items():
        if normalized.startswith(prefix):
            return product
    return "other:" + hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]


def _append_record(path: Path, record: dict) -> str:
    line = (json.dumps(record, ensure_ascii=True) + "\n").encode("ascii")
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        if os.lseek(fd, 0, os.SEEK_END):
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n" and os.write(fd, b"\n") != 1:
                raise OSError("Short write while repairing audit line ending.")
        if os.write(fd, line) != len(line):
            raise OSError("Short write while appending audit entry.")
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()


def _load_head(base: Path) -> dict | None:
    try:
        raw = (base / _HEAD_FILE).read_bytes()
    except FileNotFoundError:
        return None
    head = _parse_record(raw)
    if (head is None or not isinstance(head.get("hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", head["hash"])
            or type(head.get("count")) is not int or head["count"] < 1
            or not isinstance(head.get("ts"), str) or not head["ts"].endswith("Z")):
        raise ValueError("Invalid audit head checkpoint.")
    return head


def _entries_since_head(files: list[Path], anchor: dict | None, head: dict | None) -> int:
    """Count legacy records or recover an append interrupted before head.json was replaced."""
    found = head is None or (anchor is not None and anchor["prev_of_first_kept"] == head["hash"])
    count = head["count"] if head else 0
    for path in files:
        with path.open("rb") as f:
            for raw in f:
                line = raw.strip()
                if found:
                    count += _parse_record(line) is not None
                elif hashlib.sha256(line).hexdigest() == head["hash"]:
                    found = True
    if not found:
        raise ValueError(_TAIL_MISSING)
    return count


def _write_head(base: Path, head: dict) -> None:
    # A local attacker can rewrite both the log and this checkpoint. It
    # detects accidental loss and simple deletion of the audit tail.
    path = base / _HEAD_FILE
    temp = path.with_suffix(".json.tmp")
    with temp.open("wb") as f:
        f.write(json.dumps(head, ensure_ascii=True).encode("ascii") + b"\n")
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def log_decision(
    decision: Decision,
    *,
    shots: tuple[bytes, ...] = (),
    text: tuple[str, ...] = (),
    profile: str = "work",
    client: str | None = None,
    dir_path: Path | None = None,
) -> dict:
    """Append one JSON line to the monthly audit log.

    NEVER logs window titles, OCR text, file paths, image pixels or returned text.
    Uses an OS-level lock so concurrent server processes don't interleave or break the chain.
    """
    base = dir_path or audit_dir()
    base.mkdir(parents=True, exist_ok=True)
    sent_shots = shots if decision.approved else ()
    hashes = [hashlib.sha256(b).hexdigest() for b in sent_shots if isinstance(b, (bytes, bytearray))]

    with AuditLock(base / ".lock"):
        now_utc = datetime.now(timezone.utc)
        files = _monthly_files(base)
        anchor = _read_anchor(base)
        target_file = base / f"audit-{now_utc:%Y-%m}.jsonl"
        if files:
            target_file = max(target_file, files[-1])
        elif anchor and f"{now_utc:%Y-%m}" <= anchor["pruned_through"]:
            year, month = map(int, anchor["pruned_through"].split("-"))
            year_delta, next_month = divmod(month, 12)
            target_file = base / f"audit-{year + year_delta:04d}-{next_month + 1:02d}.jsonl"
        prev, recovered = _find_prev_hash(files, anchor)
        tail_missing = False
        try:
            head = _load_head(base)
            count = head["count"] if head and head["hash"] == prev else _entries_since_head(files, anchor, head)
        except (OSError, ValueError):
            # head.json is unreadable or names a line that's gone (restored backup,
            # sync conflict, deleted month). Keep logging: chain on from what's on
            # disk and mark this entry so `argus audit verify` reports the gap.
            tail_missing = True
            count = _entries_since_head(files, anchor, None)
        record = {
            "ts": _utc_stamp(now_utc),
            "profile": profile,
            "tool": decision.tool,
            "decision": "sent" if decision.approved else "denied",
            "reason": decision.reason,
            "apps": list(decision.apps),
            "images": len(hashes),
            "image_sha256": hashes,
            "text_chars": sum(len(t) for t in text if isinstance(t, str)),
            "request_id": decision.request_id,
            "client": _clean_client(client),
            "argus_version": __version__,
            "prev": prev,
        }
        if recovered:
            record["recovered"] = True
        if tail_missing:
            record["tail_missing"] = True
        line_hash = _append_record(target_file, record)
        _write_head(base, {"hash": line_hash, "count": count + 1, "ts": record["ts"]})

    return record


def _matches_since(ts: str, since: str) -> bool:
    return (ts[:10] if len(since) == 10 else ts) >= since


def read_entries(
    *,
    since: str | None = None,
    limit: int | None = 20,
    dir_path: Path | None = None,
) -> list[dict]:
    """Read audit entries in append order across monthly files."""
    base = dir_path or (home() / "audit")
    if not base.exists():
        return []
    since = since.strip() if since else None
    entries = []
    for path in _monthly_files(base):
        try:
            with path.open("rb") as f:
                for raw in f:
                    data = _parse_record(raw.strip())
                    if data is None:
                        continue
                    ts = data.get("ts")
                    if not since or (isinstance(ts, str) and _matches_since(ts, since)):
                        entries.append(data)
        except OSError:
            continue

    if limit is not None and limit > 0 and len(entries) > limit and not since:
        return entries[-limit:]
    return entries


def verify_chain(dir_path: Path | None = None) -> tuple[bool, str]:
    """Check the tamper-evidence hash chain across all monthly audit files.

    Reports recovered crash fragments and the first broken hash link.
    """
    base = dir_path or (home() / "audit")
    if not base.exists():
        return True, "No audit logs found."
    try:
        with AuditLock(base / ".lock"):
            return _verify_locked(base)
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"Could not verify audit log. {exc}"


def _verify_locked(base: Path) -> tuple[bool, str]:
    files = _monthly_files(base)
    anchor = _read_anchor(base)
    expected_prev = anchor["prev_of_first_kept"] if anchor else "genesis"
    if anchor and files and files[0].name[6:13] <= anchor["pruned_through"]:
        return False, "Invalid audit pruning anchor month for the retained files."
    notices = [f"Audit logs pruned through {anchor['pruned_through']}."] if anchor else []
    invalid_at = None
    total_entries = 0
    gaps: list[str] = []  # entries written after head.json showed lost entries

    for path in files:
        with path.open("rb") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                where = f"{path.name}:{line_no}"
                data = _parse_record(line)
                if data is None or not raw.endswith(b"\n"):
                    notices.append(f"Warning: {path.name}: incomplete line {line_no} (crash?)")
                if data is None:
                    if raw.endswith(b"\n") and invalid_at is None:
                        invalid_at = where
                    continue
                if invalid_at and data.get("recovered") is not True:
                    return False, "\n".join([f"Chain broken at {invalid_at} (invalid JSON or UTF-8).", *notices])
                invalid_at = None
                if data.get("prev") != expected_prev:
                    expected = "genesis prev" if expected_prev == "genesis" else f"prev {expected_prev}"
                    return False, "\n".join([f"Chain broken at {where} (expected {expected}).", *notices])
                if data.get("tail_missing") is True:
                    gaps.append(where)
                expected_prev = hashlib.sha256(line).hexdigest()
                total_entries += 1

    if invalid_at:
        return False, "\n".join([f"Chain broken at {invalid_at} (invalid JSON or UTF-8).", *notices])
    head = _load_head(base)
    if head is not None and head["hash"] != expected_prev:
        return False, "\n".join([_TAIL_MISSING, *notices])
    if gaps:
        lost = f"{_TAIL_MISSING} Entries were lost before {', '.join(gaps)}; logging resumed there."
        return False, "\n".join([lost, *notices])
    if not files:
        summary = "No audit logs found."
    elif not total_entries:
        summary = "Audit log is empty."
    else:
        summary = f"Verified {total_entries} entries across {len(files)} file(s): chain valid."
    return True, "\n".join([summary, *notices])


def export_log(
    out_path: Path, *, since: str | None = None, dir_path: Path | None = None,
    report_skipped: Callable[[int], None] | None = None,
) -> int:
    """Export audit entries to a single JSONL file for compliance."""
    base = dir_path or (home() / "audit")
    out_path = Path(out_path)
    if (Path(os.path.abspath(out_path)).is_relative_to(Path(os.path.abspath(base)))
            or out_path.resolve().is_relative_to(base.resolve())):
        raise ValueError("Choose an export path outside the audit directory.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    since = since.strip() if since is not None else None
    count = 0
    skipped = 0
    with out_path.open("wb") as out_f:
        for path in _monthly_files(base):
            with path.open("rb") as in_f:
                for line in in_f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if since is not None:
                        data = _parse_record(stripped)
                        ts = data.get("ts") if data is not None else None
                        if not isinstance(ts, str):
                            skipped += 1
                            continue
                        if not _matches_since(ts, since):
                            continue
                    out_f.write(stripped + b"\n")
                    count += 1
    if since is not None and report_skipped is not None:
        report_skipped(skipped)
    return count


def cleanup_audit(retention_months: int = 12, dir_path: Path | None = None) -> int:
    """Delete monthly audit files older than retention_months."""
    base = dir_path or (home() / "audit")
    if not base.exists():
        return 0
    removed = 0
    with AuditLock(base / ".lock"):
        now_utc = datetime.now(timezone.utc)
        anchor = _read_anchor(base)
        prev = anchor["prev_of_first_kept"] if anchor else "genesis"
        pending = base / _PENDING_ANCHOR
        temp = base / "anchor.json.tmp"
        for path in _monthly_files(base):
            month_str = path.name[6:13]
            year, month = map(int, month_str.split("-"))
            age_months = (now_utc.year - year) * 12 + (now_utc.month - month)
            if age_months <= retention_months:
                break
            try:
                last, _ = _get_last_line(path)
            except OSError:
                break
            if last is not None:
                prev = hashlib.sha256(last).hexdigest()
            anchor = {"prev_of_first_kept": prev, "pruned_through": month_str, "ts": _utc_stamp(now_utc)}
            with temp.open("wb") as f:
                f.write(json.dumps(anchor, ensure_ascii=True).encode("ascii") + b"\n")
                f.flush()
                os.fsync(f.fileno())
            temp.replace(pending)
            try:
                path.unlink()
            except OSError:
                # Keep the anchor at the end of the contiguous pruned prefix.
                pending.unlink()
                break
            pending.replace(base / _ANCHOR_FILE)
            removed += 1
    return removed


def format_table(entries: list[dict]) -> str:
    """Format audit entries as a human-readable table."""
    if not entries:
        return "No audit log entries found."
    headers = ("Time", "Tool", "Decision", "Reason", "Apps")
    rows = []
    for e in entries:
        ts = e.get("ts", "")
        t_str = ts.replace("T", " ").rstrip("Z") if ts else "-"
        tool = str(e.get("tool", "-"))
        decision = str(e.get("decision", "-"))
        reason = str(e.get("reason", "-"))
        apps_list = e.get("apps", [])
        apps = ", ".join(apps_list) if apps_list else "(none)"
        rows.append((t_str, tool, decision, reason, apps))

    w_time = max(len(headers[0]), max((len(r[0]) for r in rows), default=19))
    w_tool = max(len(headers[1]), max((len(r[1]) for r in rows), default=4))
    w_dec = max(len(headers[2]), max((len(r[2]) for r in rows), default=8))
    w_reas = max(len(headers[3]), max((len(r[3]) for r in rows), default=6))

    header_line = f"{headers[0]:<{w_time}}  {headers[1]:<{w_tool}}  {headers[2]:<{w_dec}}  {headers[3]:<{w_reas}}  {headers[4]}"
    sep_line = "-" * len(header_line)
    lines = [header_line, sep_line]
    for r in rows:
        lines.append(f"{r[0]:<{w_time}}  {r[1]:<{w_tool}}  {r[2]:<{w_dec}}  {r[3]:<{w_reas}}  {r[4]}")
    return "\n".join(lines)
