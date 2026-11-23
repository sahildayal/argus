"""The MCP wiring: schemas, content blocks, and error handling, against a stub
session (no screen access)."""

import asyncio
import base64
import io

import pytest
from mcp import Client
from PIL import Image

from argus import config, gate, server, service
from argus.capture import Shot
from argus.config import set_profile
from argus.desktop import NO_HOST, ArgusError
from argus.imaging import render
from argus.service import Result, Session
from argus.store import Store
from argus.text import display_arg


class StubSession:
    def __init__(self):
        self.store = Store()

    def _cap(self, color="navy"):
        shot = Shot(image=Image.new("RGB", (640, 400), color), kind="window", label='Google Chrome - "Test" on Monitor 2',
                    origin=(100, 100), target={"window": 1}, cursor=(150, 150))
        view, _ = render(shot.image, max_side=2000, cursor=(50, 50))
        return self.store.add(shot, view)

    def look(self, scope, grid, fresh):
        return Result().text("Picked X because it's the active window.").add(self._cap()).text("Other candidates - none.")

    def screenshot(self, monitor, window, region, grid):
        if monitor == "9":
            raise ArgusError("There is no monitor 9.")
        if monitor == "boom":
            raise RuntimeError("kaboom")
        if monitor == "phi":
            raise KeyError("Doe, John (MRN 123456) - Epic")
        if monitor == "path":
            from argus.config import home

            raise ArgusError(f"No such file: {home() / 'captures' / '2026-11-01' / '120000_c1_doe-john-chart.png'}")
        return Result().add(self._cap())

    def list_screens(self):
        return Result().text("Monitors (numbered left to right): ...")


@pytest.fixture
def stub(monkeypatch):
    s = StubSession()
    monkeypatch.setattr(server, "_session", s)
    return s


async def _with_client(fn):
    async with Client(server.mcp) as client:
        return await fn(client)


def call(name, args=None):
    return asyncio.run(_with_client(lambda c: c.call_tool(name, args or {})))


def test_tools_are_listed_with_useful_schemas():
    tools = asyncio.run(_with_client(lambda c: c.list_tools())).tools
    names = {t.name for t in tools}
    assert names == {"look", "screenshot", "zoom", "list_screens", "latest_snip", "latest_mark", "read_text", "find_text",
                     "save_baseline", "compare", "wait_for"}
    by_name = {t.name: t for t in tools}
    props = by_name["screenshot"].input_schema["properties"]
    assert {"monitor", "window", "region", "grid"} <= set(props)
    assert "leftmost" in props["monitor"].get("description", "") or "1 = leftmost" in str(props["monitor"])
    assert "ctx" not in by_name["wait_for"].input_schema["properties"]
    for t in tools:
        assert t.description and len(t.description) > 60
        assert t.annotations is not None
        assert t.annotations.read_only_hint is (t.name != "save_baseline")


def test_server_instructions_explain_look_first():
    assert "call look first" in server.mcp.instructions


def test_look_returns_caption_and_image(stub):
    r = call("look")
    assert not r.is_error
    assert [c.type for c in r.content] == ["text", "text", "image", "text"]
    img = r.content[2]
    assert img.mime_type == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(img.data))).size == (640, 400)
    assert r.content[1].text.startswith("[c1] Google Chrome")
    assert "File:" in r.content[1].text


def test_argus_errors_reach_the_model_verbatim(stub):
    r = call("screenshot", {"monitor": "9"})
    assert r.is_error and "There is no monitor 9" in r.content[0].text


def test_unexpected_errors_are_summarised_not_hidden(stub):
    r = call("screenshot", {"monitor": "boom"})
    assert r.is_error and "unexpected error" in r.content[0].text and "kaboom" in r.content[0].text


@pytest.mark.parametrize("profile", ["work", "personal"])
@pytest.mark.parametrize("tool", ["screenshot", "read_text"])
def test_exception_logging_respects_gate_policy(stub, caplog, profile, tool, monkeypatch):
    import logging
    import re

    async def read_text(*_args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(stub, "read_text", read_text, raising=False)
    set_profile(profile)
    caplog.set_level(logging.ERROR, logger="argus")
    result = call(tool, {"monitor": "boom"})
    (record,) = [r for r in caplog.records if r.name == "argus"]
    if profile == "work":
        message = record.getMessage()
        match = re.search(r"RuntimeError \[error id ([0-9a-f]+)\]", message)
        assert match and match[1] in result.content[0].text
        assert not record.exc_info and "kaboom" not in caplog.text
    else:
        assert record.exc_info and "RuntimeError: kaboom" in caplog.text


def test_exception_logging_stays_private_when_gate_enabled_mid_call(stub, caplog, monkeypatch):
    def fail(*_args):
        set_profile("work")
        raise RuntimeError("private error text")

    monkeypatch.setattr(stub, "screenshot", fail)
    result = call("screenshot")
    (record,) = [r for r in caplog.records if r.name == "argus"]
    assert "error id" in result.content[0].text
    assert not record.exc_info and "private error text" not in caplog.text


@pytest.mark.parametrize(("raw", "clean"), [
    ("  patient\t\r\n  portal\u2028again\u00a0 ", "patient portal again"),
    ("a\x00\x1b\x7f\u0081b", "ab"),
    ("x\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff\u034fy", "xy"),
    ("x\ufe0f\U000e0100y", "xy"),
    ("a" * 201, "a" * 199 + "…"),
])
def test_display_arg(raw, clean):
    assert display_arg(raw) == clean


def test_bad_arguments_are_rejected(stub):
    assert call("zoom", {"box": [1, 2, 3]}).is_error
    assert call("latest_snip", {"count": 50}).is_error


def test_list_screens_is_text_only(stub):
    r = call("list_screens")
    assert [c.type for c in r.content] == ["text"] and "Monitors" in r.content[0].text


def test_unapproved_result_and_error_contain_no_screen_data(stub, monkeypatch):
    set_profile("work")
    seen = []

    def deny(preview):
        seen.append(preview)
        return "denied"

    monkeypatch.setattr(gate, "_transport", deny)
    for name, args in [("look", {}), ("list_screens", {}), ("screenshot", {"monitor": "9"})]:
        r = call(name, args)
        assert [(c.type, c.text) for c in r.content] == [("text", gate.DECLINED)]
    assert b"PNG" in seen[0].images[0][:8]
    assert "Google Chrome" in seen[0].text[1]
    assert seen[1].text == ("Monitors (numbered left to right): ...",)
    assert seen[2].text == ("There is no monitor 9.",)


def test_unavailable_gate_is_an_mcp_error(stub, monkeypatch):
    set_profile("work")

    def unavailable(_preview):
        raise OSError("internal transport details")

    monkeypatch.setattr(gate, "_transport", unavailable)
    r = call("look")
    assert r.is_error
    assert len(r.content) == 1
    assert gate.UNAVAILABLE in r.content[0].text
    assert "internal transport details" not in r.content[0].text


@pytest.fixture
def capture_session(monkeypatch):
    set_profile("work")
    windows = frozenset({(10, "example.exe", 100)})
    shot = Shot(Image.new("RGB", (40, 32), "navy"), "region", "Private text", (0, 0),
                {"region": [0, 0, 40, 32]}, profile="work", share_windows=windows,
                policy=config.settings().policy)
    s = Session(host=NO_HOST, track=False)
    monkeypatch.setattr(server, "_session", s)
    monkeypatch.setattr(s, "desk", lambda: None)
    monkeypatch.setattr(service, "capture", lambda *_a: [shot])
    return s, shot


def test_approved_capture_allows_zoom_and_ocr(capture_session, monkeypatch):
    seen = []

    def approve(preview):
        seen.append(preview)
        return "user"

    async def read(_image):
        return []

    monkeypatch.setattr(gate, "_transport", approve)
    monkeypatch.setattr(service.ocr, "read", read)
    result = call("screenshot")
    assert [c.type for c in result.content] == ["text", "image"]
    assert seen[0].text == (result.content[0].text,)
    assert seen[0].images == (base64.b64decode(result.content[1].data),)
    assert not call("zoom", {"capture_id": "c1", "box": [0, 0, 10, 10]}).is_error
    assert not call("read_text", {"capture_id": "c1"}).is_error
    assert not call("find_text", {"capture_id": "c1", "text": "private"}).is_error
    assert [p.tool for p in seen] == ["screenshot"]


def test_all_approved_image_blocks_match_the_preview(stub, monkeypatch):
    set_profile("work")
    seen = []
    monkeypatch.setattr(stub, "screenshot", lambda *_: Result().add(stub._cap("navy")).add(stub._cap("green")))
    monkeypatch.setattr(gate, "_transport", lambda p: seen.append(p) or "user")

    result = call("screenshot")
    returned = tuple(base64.b64decode(c.data) for c in result.content if c.type == "image")
    assert len(returned) == 2 and returned[0] != returned[1]
    assert seen[0].images == returned


def test_find_query_is_separate_from_screen_content(capture_session, monkeypatch):
    raw = "shift\n\t\u202eroster\u200b " + "x" * 300
    seen = []

    async def read(_image):
        return []

    def find(_lines, text):
        assert text == raw  # The original argument still drives OCR matching.
        return [service.ocr.Match("Screen match", "Screen match", (1, 1, 6, 6), 1.0)]

    monkeypatch.setattr(service.ocr, "read", read)
    monkeypatch.setattr(service.ocr, "find", find)
    monkeypatch.setattr(gate, "_transport", lambda p: seen.append(p) or "user")
    result = call("find_text", {"text": raw})

    (preview,) = seen
    assert len(preview.query) == 200 and preview.query.startswith("shift roster ")
    assert result.content[0].text == f"Requested by the AI: {preview.query}"
    assert preview.text == tuple(c.text for c in result.content[1:] if c.type == "text")
    assert "Screen match" in preview.text[0] and all("shift roster" not in part for part in preview.text)
    assert preview.images == (base64.b64decode(result.content[-1].data),)


def test_window_selector_echo_is_sanitized_in_preview_and_error(capture_session, monkeypatch):
    from argus import capture
    from conftest import desk, mon

    session, _ = capture_session
    seen = []
    monkeypatch.setattr(session, "desk", lambda: desk([mon(1, 0, 0, 100, 100)], []))
    monkeypatch.setattr(service, "capture", capture.capture)
    monkeypatch.setattr(gate, "_transport", lambda p: seen.append(p) or "user")
    raw = "unknown\n\u202e\u200bwindow"
    result = call("screenshot", {"window": raw})
    assert result.is_error and 'No window matches "unknown window"' in result.content[0].text
    assert seen[0].text == ('No window matches "unknown window". Open windows: none',)


def test_ocr_approval_does_not_approve_the_source_image(capture_session, monkeypatch):
    seen = []

    def answer(preview):
        seen.append(preview)
        return "user" if preview.tool == "read_text" else "denied"

    async def read(_image):
        return []

    monkeypatch.setattr(gate, "_transport", answer)
    monkeypatch.setattr(service.ocr, "read", read)
    call("read_text")
    result = call("zoom", {"capture_id": "c1", "box": [0, 0, 10, 10]})
    assert [p.tool for p in seen] == ["read_text", "zoom"]
    assert "Private text" in seen[0].text[0] and not seen[0].images
    assert [(c.type, c.text) for c in result.content] == [("text", gate.DECLINED)]


def test_baseline_local_receipt_and_cli_session_bypass(capture_session, monkeypatch):
    s, _ = capture_session

    def fail(_preview):
        pytest.fail("local save and CLI must not use the approval pipe")

    monkeypatch.setattr(gate, "_transport", fail)
    result = call("save_baseline", {"name": "reference"})
    assert [(c.type, c.text) for c in result.content] == [("text", "Saved baseline 'reference'.")]
    assert service.testing.load_baseline("reference").label == "Private text"
    assert s.screenshot().captures[0].shot.label == "Private text"


def test_latest_snip_gates_clipboard_image(capture_session, monkeypatch):
    from argus.snips import Snip

    _, shot = capture_session
    monkeypatch.setattr(service, "latest_snips", lambda *_a, **kw: [Snip(shot.image, "clipboard", None, shot.taken)])
    seen = []

    def answer(preview):
        seen.append(preview)
        return "denied"

    monkeypatch.setattr(gate, "_transport", answer)
    result = call("latest_snip")
    assert [(c.type, c.text) for c in result.content] == [("text", gate.DECLINED)]
    assert seen[0].images and seen[0].windows is None
    assert "image on the clipboard" in seen[0].text[0]


def test_text_only_compare_is_gated(capture_session, monkeypatch):
    _, shot = capture_session
    call("save_baseline", {"name": "reference"})
    monkeypatch.setattr(service, "recapture", lambda *_a: shot)

    def answer(preview):
        assert not preview.images
        assert preview.text[0].startswith("No visible change")
        return "denied"

    monkeypatch.setattr(gate, "_transport", answer)
    result = call("compare", {"baseline": "reference"})
    assert [(c.type, c.text) for c in result.content] == [("text", gate.DECLINED)]


@pytest.mark.parametrize(("second_reply", "denied"), [("user", False), ("timeout", True)])
def test_wait_prompts_again_on_window_change(capture_session, monkeypatch, second_reply, denied):
    from dataclasses import replace

    _, shot = capture_session
    new = replace(shot, share_windows=shot.share_windows | {(20, "other.exe", 200)})
    frames = iter([shot, new])
    seen = []

    def answer(preview):
        seen.append(preview)
        return "user" if len(seen) == 1 else second_reply

    async def read(_image):
        return []

    remaining = iter([[], [], [object()]])
    original_wait = service.testing.wait_for

    async def no_sleep(_seconds):
        pass

    async def run_wait(*args, **kwargs):
        return await original_wait(*args, **kwargs, sleep=no_sleep)

    monkeypatch.setattr(service, "recapture", lambda *_a: next(frames))
    monkeypatch.setattr(service.testing, "wait_for", run_wait)
    monkeypatch.setattr(service.ocr, "read", read)
    monkeypatch.setattr(
        service.ocr, "find",
        lambda *_a: [service.ocr.Match("done", "done", (1, 1, 6, 6), 1.0)] if next(remaining) else [],
    )
    monkeypatch.setattr(gate, "_transport", answer)
    result = call("wait_for", {"until": "text", "text": "d\u200bo\u202ene\n\t now"})
    assert [p.windows for p in seen] == [shot.share_windows, new.share_windows]
    assert [p.query for p in seen] == ["done now", "done now"]
    assert seen[0].watching and not seen[1].watching and not seen[1].allow_trust
    if denied:
        assert [(c.type, c.text) for c in result.content] == [("text", gate.DECLINED)]
    else:
        assert not result.is_error
        assert [c.type for c in result.content] == ["text", "text", "text", "image"]
        assert result.content[0].text == "Requested by the AI: done now"
        assert seen[1].text == tuple(c.text for c in result.content[1:] if c.type == "text")
        assert seen[1].images == (base64.b64decode(result.content[-1].data),)
WORK = '[profile]\nactive = "work"\n[work]\nallow_apps = ["Code.exe"]\n'


def _work_profile(argus_home):
    from argus import config

    argus_home.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(WORK, encoding="utf-8")


def test_work_profile_hides_unexpected_error_details(stub, argus_home, caplog):
    _work_profile(argus_home)
    r = call("screenshot", {"monitor": "phi"})
    text = r.content[0].text
    assert r.is_error and "error id" in text and "MRN" not in text and "Doe" not in text
    assert "KeyError" in caplog.text and "MRN" not in caplog.text and "Doe" not in caplog.text


def test_work_profile_scrubs_data_paths_from_errors(stub, argus_home, monkeypatch):
    _work_profile(argus_home)
    seen = []

    def approve(preview):
        seen.append(preview)
        return "user"

    monkeypatch.setattr(gate, "_transport", approve)
    r = call("screenshot", {"monitor": "path"})
    assert r.is_error and "doe-john" not in r.content[0].text and "<Argus data folder>" in r.content[0].text
    assert seen and "doe-john" not in seen[0].text[0]  # the preview gets the scrubbed text too


def test_personal_profile_errors_are_unchanged(stub, argus_home):
    r = call("screenshot", {"monitor": "path"})
    assert r.is_error and "doe-john" in r.content[0].text
