import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from argus import gate
from argus.config import set_profile


@pytest.mark.live
def test_tray_serializes_prompts_and_revokes_trust():
    import tkinter

    from argus.gate_ui import Approvals

    set_profile("work")
    root = tkinter.Tk()
    root.withdraw()
    approvals = Approvals(root)
    current = gate.policy()
    deadline = time.monotonic() + 10

    def preview(request_id, hwnd):
        return gate.Preview(request_id, "screenshot", (), ("Synthetic caption",),
                            frozenset({(hwnd, "test.exe", 1)}), current.stamp, deadline, True, False)

    def wait_until_queued():
        while approvals.queue.empty():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    def button(label):
        frame = next(child for child in approvals.window.winfo_children() if child.winfo_class() == "TFrame")
        return next(child for child in frame.winfo_children() if child.cget("text").startswith(label))

    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            a = workers.submit(approvals.answer, preview("first", 1))
            wait_until_queued()
            approvals.poll()
            original = approvals.window
            b = workers.submit(approvals.answer, preview("second", 2))
            wait_until_queued()
            approvals.poll()
            assert approvals.window is original
            assert approvals.active.preview.request_id == "first"
            assert button("Send").bind("<Return>")
            button("Send & trust").invoke()
            assert a.result(1) == "user"
            assert approvals.trust.covers(preview("same-window", 1), current)
            approvals.trust.revoke()
            assert not approvals.trust.covers(preview("same-window", 1), current)
            approvals.poll()
            assert approvals.active.preview.request_id == "second"
            button("Deny").invoke()
            assert b.result(1) == "denied"
        finally:
            approvals.close()
            root.destroy()
