"""The MCP wiring: schemas, content blocks, and error handling, against a stub
session (no screen access)."""

import asyncio
import base64
import io

import pytest
from mcp import Client
from PIL import Image

from argus import server
from argus.capture import Shot
from argus.desktop import ArgusError
from argus.imaging import render
from argus.service import Result
from argus.store import Store


class StubSession:
    def __init__(self):
        self.store = Store()

    def _cap(self):
        shot = Shot(image=Image.new("RGB", (640, 400), "navy"), kind="window", label='Google Chrome - "Test" on Monitor 2',
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


def test_bad_arguments_are_rejected(stub):
    assert call("zoom", {"box": [1, 2, 3]}).is_error
    assert call("latest_snip", {"count": 50}).is_error


def test_list_screens_is_text_only(stub):
    r = call("list_screens")
    assert [c.type for c in r.content] == ["text"] and "Monitors" in r.content[0].text
