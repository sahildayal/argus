"""Real overlapped pipe I/O without opening any desktop windows."""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")


def test_round_trip_and_pid_verification(monkeypatch):
    from argus import gate, gate_pipe
    from argus.config import set_profile

    set_profile("work")
    received = []

    def answer(preview):
        received.append(preview)
        return "user"

    pipe = gate_pipe.PipeServer(answer)
    pipe.start()
    try:
        # A same-user pipe owned by this Python process isn't an Argus tray.
        assert gate.request("screenshot").reason == "tray_unavailable"
        assert not received

        # Accept this test process only at the executable/ancestry check. The
        # live GetNamedPipeServerProcessId and user SID checks still run.
        monkeypatch.setattr(gate_pipe, "_is_tray", lambda process: process.pid == os.getpid())
        data = b"redacted pixels" * 70_000  # exceeds both the pipe buffer and I/O chunk
        with ThreadPoolExecutor(max_workers=3) as pool:
            calls = [pool.submit(gate.request, "screenshot", (data,), (str(i),), query=f"Query {i}") for i in range(3)]
            assert all(call.result(5).reason == "user" for call in calls)
        assert {preview.text for preview in received} == {("0",), ("1",), ("2",)}
        assert {preview.query for preview in received} == {"Query 0", "Query 1", "Query 2"}
        assert all(preview.images == (data,) and preview.deadline > time.monotonic() for preview in received)
    finally:
        pipe.close()


@pytest.mark.parametrize("name", ["argus-tray", "argus"])
def test_launchers_ignore_path(name, tmp_path, monkeypatch):
    from argus import gate_pipe

    prefix = tmp_path / "venv"
    scripts = prefix / "Scripts"
    scripts.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path_entry = tmp_path / "path-entry"
    path_entry.mkdir()
    for folder in (scripts, runtime, path_entry):
        (folder / f"{name}.exe").touch()
    monkeypatch.setenv("PATH", str(path_entry))
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(runtime / "python.exe"))
    assert gate_pipe._launchers(name) == {(scripts / f"{name}.exe").resolve(), (runtime / f"{name}.exe").resolve()}


def test_mismatched_tray_versions_fail_closed():
    """An old tray listens on a different pipe. A new tray rejects an old policy stamp."""
    import dataclasses

    from argus import gate, gate_pipe
    from argus.config import set_profile

    set_profile("work")
    assert gate.request("screenshot", (b"pixels",), ("text",)).reason == "tray_unavailable"

    good = {f.name: None for f in dataclasses.fields(gate.Preview)}
    good.update(request_id="0" * 32, tool="screenshot", deadline=time.monotonic() + 30,
                stamp=[[0, 0, 0], [0, 0, 0], [0, 0, 0], "work", "a" * 32],
                allow_trust=False, watching=False, windows=None, images=[], text=["t"], query="")
    gate_pipe._parse(dict(good))  # the current shape parses
    old = dict(good, stamp=good["stamp"][:2])
    with pytest.raises(ValueError):
        gate_pipe._parse(old)
