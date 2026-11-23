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
            calls = [pool.submit(gate.request, "screenshot", (data,), (str(i),)) for i in range(3)]
            assert all(call.result(5).reason == "user" for call in calls)
        assert {preview.text for preview in received} == {("0",), ("1",), ("2",)}
        assert all(preview.images == (data,) and preview.deadline > time.monotonic() for preview in received)
    finally:
        pipe.close()
