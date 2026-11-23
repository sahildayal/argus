"""Append-only, tamper-evident audit log of gate disclosure decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextvars import ContextVar
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

_current_client: ContextVar[str | None] = ContextVar("audit_current_client", default=None)


def set_current_client(name: str | None):
    return _current_client.set(name)


def get_current_client() -> str | None:
    return _current_client.get()


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT)
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


def _get_last_line(path: Path) -> str | None:
    """Read the last non-empty line of a file efficiently without loading the whole file."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        buf = bytearray()
        block_size = 4096
        pos = size
        while pos > 0:
            step = min(block_size, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            buf = bytearray(chunk) + buf
            lines = buf.split(b"\n")
            non_empty = [line.strip() for line in lines if line.strip()]
            if non_empty and (pos == 0 or len(lines) > 2):
                return non_empty[-1].decode("utf-8")
        if buf.strip():
            return buf.strip().decode("utf-8")
        return None


def _find_prev_hash(target_file: Path, base_dir: Path) -> str:
    """Find the SHA-256 hash of the previous line (current file, latest prior monthly file, or 'genesis')."""
    if target_file.exists():
        last = _get_last_line(target_file)
        if last:
            return hashlib.sha256(last.strip().encode("utf-8")).hexdigest()

    prior_files = sorted(
        [p for p in base_dir.glob("audit-*.jsonl") if p.is_file() and p.name < target_file.name],
        key=lambda p: p.name,
        reverse=True,
    )
    for pf in prior_files:
        last = _get_last_line(pf)
        if last:
            return hashlib.sha256(last.strip().encode("utf-8")).hexdigest()

    return "genesis"


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

    now_utc = datetime.fromtimestamp(decision.when, timezone.utc)
    month_str = now_utc.strftime("%Y-%m")
    target_file = base / f"audit-{month_str}.jsonl"

    client_name = client if client is not None else get_current_client()

    with AuditLock(base / ".lock"):
        prev = _find_prev_hash(target_file, base)
        record = {
            "ts": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile": profile,
            "tool": decision.tool,
            "decision": "sent" if decision.approved else "denied",
            "reason": decision.reason,
            "apps": list(decision.apps),
            "images": len(shots),
            "image_sha256": [hashlib.sha256(b).hexdigest() for b in shots if isinstance(b, (bytes, bytearray))],
            "text_chars": sum(len(t) for t in text if isinstance(t, str)),
            "request_id": decision.request_id,
            "client": client_name,
            "argus_version": __version__,
            "prev": prev,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

    return record


def _matches_since(ts_str: str, since: str) -> bool:
    since_clean = since.strip()
    if len(since_clean) == 10:
        return ts_str[:10] >= since_clean
    return ts_str >= since_clean


def read_entries(
    *,
    since: str | None = None,
    limit: int | None = 20,
    dir_path: Path | None = None,
) -> list[dict]:
    """Read audit entries in chronological order across monthly files."""
    base = dir_path or (home() / "audit")
    if not base.exists():
        return []
    files = sorted([p for p in base.glob("audit-*.jsonl") if p.is_file()])
    entries = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as in_f:
                for line in in_f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        data = json.loads(stripped)
                    except Exception:
                        continue
                    if since and not _matches_since(data.get("ts", ""), since):
                        continue
                    entries.append(data)
        except Exception:
            continue

    if limit is not None and limit > 0 and len(entries) > limit and not since:
        return entries[-limit:]
    return entries


def verify_chain(dir_path: Path | None = None) -> tuple[bool, str]:
    """Check the tamper-evidence hash chain across all monthly audit files.

    Reports the first broken line found. This is evidence, not cryptographic
    protection against local administrative tampering.
    """
    base = dir_path or (home() / "audit")
    if not base.exists():
        return True, "No audit logs found."
    files = sorted([p for p in base.glob("audit-*.jsonl") if p.is_file()])
    if not files:
        return True, "No audit logs found."

    expected_prev: str | None = None
    total_entries = 0
    files_checked = 0

    for file_path in files:
        files_checked += 1
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as exc:
            return False, f"Could not read {file_path.name}: {exc}"

        for line_no, raw_line in enumerate(lines, start=1):
            line_str = raw_line.strip()
            if not line_str:
                continue
            total_entries += 1
            try:
                data = json.loads(line_str)
            except Exception as exc:
                return False, f"Chain broken at {file_path.name}:{line_no} (invalid JSON: {exc})"

            actual_prev = data.get("prev")
            if expected_prev is None:
                if actual_prev != "genesis":
                    return False, f"Chain broken at {file_path.name}:{line_no} (expected genesis prev, got {actual_prev!r})"
            else:
                if actual_prev != expected_prev:
                    return False, f"Chain broken at {file_path.name}:{line_no} (expected prev {expected_prev}, got {actual_prev})"

            expected_prev = hashlib.sha256(line_str.encode("utf-8")).hexdigest()

    if total_entries == 0:
        return True, "Audit log is empty."
    return True, f"Verified {total_entries} entries across {files_checked} file(s): chain valid."


def export_log(out_path: Path, *, since: str | None = None, dir_path: Path | None = None) -> int:
    """Export audit entries to a single JSONL file for compliance."""
    base = dir_path or (home() / "audit")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not base.exists():
        out_path.write_text("", encoding="utf-8")
        return 0

    files = sorted([p for p in base.glob("audit-*.jsonl") if p.is_file()])
    count = 0
    with open(out_path, "w", encoding="utf-8") as out_f:
        for f in files:
            with open(f, "r", encoding="utf-8") as in_f:
                for line in in_f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if since:
                        try:
                            data = json.loads(stripped)
                            if not _matches_since(data.get("ts", ""), since):
                                continue
                        except Exception:
                            continue
                    out_f.write(stripped + "\n")
                    count += 1
    return count


def cleanup_audit(retention_months: int = 12, dir_path: Path | None = None) -> int:
    """Delete monthly audit files older than retention_months."""
    base = dir_path or (home() / "audit")
    if not base.exists():
        return 0
    now_utc = datetime.now(timezone.utc)
    removed = 0
    pattern = re.compile(r"^audit-(\d{4})-(\d{2})\.jsonl$")
    for p in base.glob("audit-*.jsonl"):
        m = pattern.match(p.name)
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        age_months = (now_utc.year - year) * 12 + (now_utc.month - month)
        if age_months >= retention_months:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
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
