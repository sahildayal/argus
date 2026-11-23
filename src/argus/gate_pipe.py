"""Local Windows transport for the tray approval gate.

One request and one reply per byte-mode pipe connection. Each message is a
big-endian uint32 length followed by UTF-8 JSON. Images are the exact MCP
payload bytes (base64), so there are no temporary preview files to clean up.
"""

from __future__ import annotations

import _winapi as win
import base64
import ctypes as ct
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes as wt
from dataclasses import asdict
from pathlib import Path

import psutil

from .config import home
from .gate import Preview
from .win32 import _fn, kernel32

advapi = ct.WinDLL("advapi32", use_last_error=True)
_open_token = _fn(advapi, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD, ct.POINTER(wt.HANDLE))
_token_info = _fn(advapi, "GetTokenInformation", wt.BOOL, wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.POINTER(wt.DWORD))
_sid_string = _fn(advapi, "ConvertSidToStringSidW", wt.BOOL, ct.c_void_p, ct.POINTER(wt.LPWSTR))
_security_descriptor = _fn(
    advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    wt.BOOL, wt.LPCWSTR, wt.DWORD, ct.POINTER(ct.c_void_p), ct.c_void_p,
)
_create_pipe = _fn(
    kernel32, "CreateNamedPipeW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD,
    wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ct.c_void_p,
)
_server_pid = _fn(kernel32, "GetNamedPipeServerProcessId", wt.BOOL, wt.HANDLE, ct.POINTER(wt.ULONG))
_session_id = _fn(kernel32, "ProcessIdToSessionId", wt.BOOL, wt.DWORD, ct.POINTER(wt.DWORD))
_open_process = _fn(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
_local_free = _fn(kernel32, "LocalFree", ct.c_void_p, ct.c_void_p)
_disconnect = _fn(kernel32, "DisconnectNamedPipe", wt.BOOL, wt.HANDLE)

_INVALID_HANDLE = ct.c_void_p(-1).value
_MAX_MESSAGE = 64 * 1024 * 1024
_CHUNK = 256 * 1024
_MAX_CLIENTS = 16
_REPLIES = ("user", "denied", "trusted", "timeout", "tray_unavailable")


class _SecurityAttributes(ct.Structure):
    _fields_ = [("length", wt.DWORD), ("descriptor", ct.c_void_p), ("inherit", wt.BOOL)]


def _sid(process: int) -> str:
    token, size, encoded = wt.HANDLE(), wt.DWORD(), wt.LPWSTR()
    if not _open_token(process, 0x0008, ct.byref(token)):  # TOKEN_QUERY
        raise ct.WinError(ct.get_last_error())
    try:
        _token_info(token, 1, None, 0, ct.byref(size))  # TokenUser
        buffer = ct.create_string_buffer(size.value)
        if not _token_info(token, 1, buffer, size, ct.byref(size)):
            raise ct.WinError(ct.get_last_error())
        sid = ct.cast(buffer, ct.POINTER(ct.c_void_p))[0]  # TOKEN_USER.User.Sid
        if not _sid_string(sid, ct.byref(encoded)):
            raise ct.WinError(ct.get_last_error())
        try:
            return encoded.value
        finally:
            _local_free(encoded)
    finally:
        win.CloseHandle(token.value)


def _session(pid: int) -> int:
    session = wt.DWORD()
    if not _session_id(pid, ct.byref(session)):
        raise ct.WinError(ct.get_last_error())
    return session.value


def _address() -> tuple[str, str]:
    sid = _sid(win.GetCurrentProcess())
    key = f"{sid}:{_session(os.getpid())}:{os.path.normcase(str(home().resolve()))}".encode()
    # Version 2 requires a separate query field in the approval preview.
    return rf"\\.\pipe\ArgusApproval-v2-{hashlib.sha256(key).hexdigest()}", sid


def _new_pipe(address: str, sid: str, *, first: bool) -> int:
    descriptor = ct.c_void_p()
    # A protected DACL with one ACE for this user's SID. Reject remote clients
    # even if Windows would authenticate them as the same user.
    if not _security_descriptor(f"D:P(A;;GA;;;{sid})", 1, ct.byref(descriptor), None):
        raise ct.WinError(ct.get_last_error())
    try:
        attrs = _SecurityAttributes(ct.sizeof(_SecurityAttributes), descriptor, False)
        mode = win.PIPE_ACCESS_DUPLEX | win.FILE_FLAG_OVERLAPPED
        if first:
            mode |= win.FILE_FLAG_FIRST_PIPE_INSTANCE
        handle = _create_pipe(address, mode, 0x8, _MAX_CLIENTS + 1, 65536, 65536, 50, ct.byref(attrs))
        if handle == _INVALID_HANDLE:
            raise ct.WinError(ct.get_last_error())
        return handle
    finally:
        _local_free(descriptor)


def _launchers(name: str) -> set[Path]:
    candidates = [Path(sys.prefix) / "Scripts" / f"{name}.exe", Path(sys.executable).with_name(f"{name}.exe")]
    return {p.resolve() for p in candidates if p.is_file()}


def _is_tray(process: psutil.Process) -> bool:
    exe = Path(process.exe()).resolve()
    args = process.cmdline()[1:]
    trays, clis = _launchers("argus-tray"), _launchers("argus")
    if exe in trays and not args:
        return True
    # uv's launcher starts Python via a venv redirector. Pin the script and
    # interpreter paths and verify the real launcher in the parent chain.
    runtimes = {
        (folder / name).resolve()
        for folder in (Path(sys.executable).parent, Path(sys.base_prefix), Path(sys.prefix) / "Scripts")
        for name in ("python.exe", "pythonw.exe")
    }
    if exe not in runtimes or not args:
        return False
    script = Path(args[0]).resolve()
    if not ((script in trays and args[1:] == []) or (script in clis and args[1:] == ["tray"])):
        return False
    parent = process.parent()
    while parent is not None and Path(parent.exe()).resolve() in runtimes:
        parent = parent.parent()
    return parent is not None and Path(parent.exe()).resolve() == script and parent.username() == process.username()


def _verify_tray(handle: int, sid: str) -> None:
    pid = wt.ULONG()
    if not _server_pid(handle, ct.byref(pid)):
        raise ct.WinError(ct.get_last_error())
    if _session(pid.value) != _session(os.getpid()):
        raise OSError("Argus tray belongs to a different Windows session")
    process_handle = _open_process(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process_handle:
        raise ct.WinError(ct.get_last_error())
    try:
        try:
            process = psutil.Process(pid.value)
            # This rejects a same-user process that merely binds the name. Code
            # running as the user can still modify/inject the tray or automate
            # clicks. This check is not a sandbox against that user account.
            if _sid(process_handle) == sid and _is_tray(process) and process.is_running():
                return
        except psutil.Error:
            pass
        raise OSError("Could not verify the Argus tray pipe server")
    finally:
        win.CloseHandle(process_handle)


def _cancel(ov) -> None:
    """Drain cancelled I/O before Python frees the overlapped buffer."""
    try:
        ov.cancel()
        ov.GetOverlappedResult(True)
    except OSError:
        pass


def _complete(ov, status: int, deadline: float) -> int:
    if status == win.ERROR_IO_PENDING:
        ms = max(0, math.ceil((deadline - time.monotonic()) * 1000))
        try:
            if win.WaitForSingleObject(ov.event, ms) != win.WAIT_OBJECT_0:
                raise TimeoutError()
        except BaseException:
            _cancel(ov)
            raise
    count, error = ov.GetOverlappedResult(True)
    if error:
        raise ct.WinError(error)
    return count


def _read(handle: int, length: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < length:
        if time.monotonic() >= deadline:
            raise TimeoutError()
        ov, status = win.ReadFile(handle, min(_CHUNK, length - len(data)), overlapped=True)
        count = _complete(ov, status, deadline)
        if not count:
            raise OSError("Incomplete approval message")
        data.extend(ov.getbuffer())
    return bytes(data)


def _receive(handle: int, deadline: float) -> dict:
    size, = struct.unpack("!I", _read(handle, 4, deadline))
    if not 0 < size <= _MAX_MESSAGE:
        raise ValueError("Invalid approval message size")
    value = json.loads(_read(handle, size, deadline))
    if type(value) is not dict:
        raise ValueError("Invalid approval message")
    return value


def _send(handle: int, value: dict, deadline: float) -> None:
    body = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    if len(body) > _MAX_MESSAGE:
        raise ValueError("Approval preview is too large")
    data = memoryview(struct.pack("!I", len(body)) + body)
    while data:
        if time.monotonic() >= deadline:
            raise TimeoutError()
        ov, status = win.WriteFile(handle, data[:_CHUNK], overlapped=True)
        count = _complete(ov, status, deadline)
        if not count:
            raise OSError("Incomplete approval message")
        data = data[count:]


def exchange(preview: Preview) -> str:
    address, sid = _address()
    while True:
        try:
            # FILE_READ_DATA | FILE_WRITE_DATA | SYNCHRONIZE, with identification
            # level SQOS (a server cannot impersonate us to access other resources).
            handle = win.CreateFile(address, 0x100003, 0, win.NULL, win.OPEN_EXISTING,
                                    win.FILE_FLAG_OVERLAPPED | 0x110000, win.NULL)
            break
        except OSError as exc:
            if exc.winerror != win.ERROR_PIPE_BUSY:
                raise
            remaining = preview.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError() from None
            time.sleep(min(0.025, remaining))
    try:
        _verify_tray(handle, sid)  # before sending even the request metadata
        value = asdict(preview)
        value["windows"] = None if preview.windows is None else sorted(preview.windows)
        value["images"] = [base64.b64encode(b).decode("ascii") for b in preview.images]
        _send(handle, value, preview.deadline)
        reply = _receive(handle, preview.deadline)
        if (set(reply) != {"request_id", "reason"} or reply["request_id"] != preview.request_id
                or reply["reason"] not in _REPLIES):
            raise ValueError("Invalid approval reply")
        return reply["reason"]
    finally:
        win.CloseHandle(handle)


def _parse(value: dict) -> Preview:
    keys = set(Preview.__dataclass_fields__)
    if value.keys() != keys:
        raise ValueError("Invalid approval request")
    rid, tool, deadline, stamp = (value[k] for k in ("request_id", "tool", "deadline", "stamp"))
    if (type(rid) is not str or not re.fullmatch(r"[0-9a-f]{32}", rid)
            or type(tool) is not str or not re.fullmatch(r"[a-z_]{1,40}", tool)
            or type(deadline) not in (int, float) or not time.monotonic() < deadline <= time.monotonic() + 86400
            or type(stamp) is not list or len(stamp) != 2
            or any(type(row) is not list or len(row) != 3 or any(type(n) is not int for n in row) for row in stamp)
            or any(type(value[k]) is not bool for k in ("allow_trust", "watching"))):
        raise ValueError("Invalid approval request")
    windows, images, text = (value[k] for k in ("windows", "images", "text"))
    if windows is not None and (
        type(windows) is not list or any(
            type(w) is not list or len(w) != 3 or type(w[0]) is not int or type(w[1]) is not str
            or type(w[2]) is not int for w in windows
        )
    ):
        raise ValueError("Invalid approval window set")
    if (type(value["query"]) is not str
            or any(type(parts) is not list or any(type(p) is not str for p in parts) for parts in (images, text))):
        raise ValueError("Invalid approval preview")
    return Preview(
        rid, tool, tuple(base64.b64decode(b, validate=True) for b in images), tuple(text),
        None if windows is None else frozenset((h, exe, pid) for h, exe, pid in windows),
        (tuple(stamp[0]), tuple(stamp[1])), deadline, value["allow_trust"], value["watching"], value["query"],
    )


class PipeServer:
    def __init__(self, answer: Callable[[Preview], str]):
        self.answer = answer
        self._address, self._sid = _address()
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(_MAX_CLIENTS)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handle = _new_pipe(self._address, self._sid, first=True)
        self._thread = threading.Thread(target=self._serve, args=(handle,), name="argus-approval-pipe", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _serve(self, listener: int) -> None:
        try:
            while not self._stop.is_set():
                try:
                    ov = win.ConnectNamedPipe(listener, overlapped=True)
                except OSError as exc:
                    if exc.winerror != win.ERROR_NO_DATA:
                        raise
                    _disconnect(listener)
                    continue
                try:
                    while win.WaitForSingleObject(ov.event, 100) == win.WAIT_TIMEOUT:
                        if self._stop.is_set():
                            _cancel(ov)
                            return
                    # CPython signals the event itself when the client connected
                    # before ConnectNamedPipe. Use the stdlib listener's waiting
                    # form to handle that immediate-connect case as well.
                    _, error = ov.GetOverlappedResult(True)
                    if error:
                        raise ct.WinError(error)
                except BaseException:
                    _cancel(ov)
                    raise
                if not self._slots.acquire(blocking=False):
                    # Keep a pipe instance alive while recycling a busy listener.
                    _disconnect(listener)
                    continue
                connected = listener
                listener = _new_pipe(self._address, self._sid, first=False)
                threading.Thread(target=self._client, args=(connected,), daemon=True).start()
        except OSError:
            # An unavailable pipe fails closed on the client. Avoid logging
            # exception details here because the pipe only carries private previews.
            pass
        finally:
            win.CloseHandle(listener)

    def _client(self, handle: int) -> None:
        try:
            preview = _parse(_receive(handle, time.monotonic() + 5))
            reason = self.answer(preview)
            _send(handle, {"request_id": preview.request_id, "reason": reason}, preview.deadline)
            # Keep the server end open until the client reads the reply and closes.
            _read(handle, 1, min(preview.deadline, time.monotonic() + 1))
        except (OSError, ValueError, OverflowError, RecursionError):
            pass
        finally:
            win.CloseHandle(handle)
            self._slots.release()


def probe(timeout: float = 1.0) -> str:
    """Read-only check for `argus status` / `argus doctor`: is a verified Argus
    tray listening on this user's approval pipe? Connects, runs the same server
    check as exchange(), and disconnects without sending anything.

    Returns "verified", "unverified" (something else owns the pipe name),
    "missing" (no tray on this pipe, e.g. none running or an older version's
    tray listening on its own name), "busy" or "error"."""
    address, sid = _address()
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = win.CreateFile(address, 0x100003, 0, win.NULL, win.OPEN_EXISTING,
                                    win.FILE_FLAG_OVERLAPPED | 0x110000, win.NULL)
            break
        except OSError as exc:
            if exc.winerror == win.ERROR_PIPE_BUSY:
                if time.monotonic() >= deadline:
                    return "busy"
                time.sleep(0.025)
                continue
            return "missing" if exc.winerror in (2, 3) else "error"  # FILE/PATH_NOT_FOUND
    try:
        _verify_tray(handle, sid)
        return "verified"
    except OSError:
        return "unverified"
    finally:
        win.CloseHandle(handle)
