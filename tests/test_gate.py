from dataclasses import replace
import time

import pytest

from argus import gate, privacy
from argus.config import config_path, set_profile


WINDOWS = frozenset({(1, "code.exe", 101), (2, "browser.exe", 202)})


@pytest.mark.parametrize(("reply", "reason", "approved"), [
    ("user", "user", True),
    ("denied", "denied", False),
    (TimeoutError(), "timeout", False),
    (FileNotFoundError(), "tray_unavailable", False),
    ("bogus", "tray_unavailable", False),
])
def test_gate_decision(reply, reason, approved):
    set_profile("work")

    def transport(preview):
        assert preview.images == (b"redacted bytes",)
        assert preview.text == ("exact caption",)
        if isinstance(reply, Exception):
            raise reply
        return reply

    decision = gate.request("screenshot", (b"redacted bytes",), ("exact caption",), windows=WINDOWS, transport=transport)
    assert (decision.reason, decision.approved, decision.tool) == (reason, approved, "screenshot")
    assert decision.apps == ["browser.exe", "code.exe"]
    assert decision.request_id and decision.when <= time.time()


def test_personal_opt_in_and_work_is_always_gated():
    seen = []

    def transport(preview):
        seen.append(preview)
        return "user"

    assert gate.request("look", transport=transport).reason == "not_required"
    assert not seen
    config_path().write_text("[gate]\nenabled = true\n", encoding="utf-8")
    assert gate.request("look", transport=transport).reason == "user"
    config_path().write_text('[profile]\nactive = "work"\n[gate]\nenabled = false\n', encoding="utf-8")
    assert gate.request("look", transport=transport).reason == "user"
    assert len(seen) == 2


def test_late_approval_is_denied(monkeypatch):
    set_profile("work")
    now = time.monotonic()
    monkeypatch.setattr(gate.time, "monotonic", lambda: now)

    def transport(preview):
        nonlocal now
        now = preview.deadline
        return "user"

    assert gate.request("look", transport=transport).reason == "timeout"


def test_trust_scope_and_expiration():
    set_profile("work")
    current = gate.policy()
    trust = gate.Trust()
    preview = gate.Preview("request", "look", (), (), WINDOWS, current.stamp, time.monotonic() + 30, True, False)
    assert trust.grant(preview, current)
    assert trust.covers(preview, current)
    assert trust.covers(replace(preview, windows=frozenset({(1, "code.exe", 101)})), current)
    assert not trust.covers(replace(preview, windows=WINDOWS | {(3, "code.exe", 101)}), current)
    assert not trust.covers(replace(preview, windows=frozenset({(1, "code.exe", 303)})), current)
    assert not trust.covers(replace(preview, windows=None), current)
    assert not trust.covers(replace(preview, windows=frozenset()), current)
    assert not trust.covers(replace(preview, allow_trust=False), current)
    trust.expires = time.monotonic() - 1
    assert not trust.covers(preview, current)
    trust.grant(preview, current)
    trust.revoke()
    assert not trust.covers(preview, current)


@pytest.mark.parametrize(("allow_trust", "windows"), [(False, WINDOWS), (True, frozenset())])
def test_trust_refuses_ineligible_requests(allow_trust, windows):
    set_profile("work")
    current = gate.policy()
    trust = gate.Trust()

    def transport(preview):
        assert not trust.grant(preview, current)
        assert trust.windows is None
        return "trusted"

    assert gate.request("look", windows=windows, allow_trust=allow_trust, transport=transport).reason == "denied"


@pytest.mark.parametrize("change", ["profile", "pause"])
def test_trust_and_in_flight_approval_are_invalid_after_state_change(change):
    set_profile("work")
    current = gate.policy()
    trust = gate.Trust()

    def transport(preview):
        trust.grant(preview, current)
        if change == "profile":
            set_profile("personal")
            set_profile("work")
        else:
            privacy.pause()
            privacy.resume()
        updated = gate.policy()
        assert not trust.covers(replace(preview, stamp=updated.stamp), updated)
        trust.refresh(updated)
        assert trust.windows is None
        return "user"

    assert gate.request("look", windows=WINDOWS, start=current, transport=transport).reason == "denied"


def test_display_arg_drops_invisible_fillers_and_mark_floods():
    from argus.text import display_arg
    fillers = "\u3164" * 50 + "\u2800\u115f\u180b\ue000"
    assert display_arg(fillers + "ok") == "ok"
    flood = display_arg("a" + "\u0301" * 150)
    assert flood.startswith("\u00e1") and len(flood) <= 3
