import asyncio

import pytest
from PIL import Image, ImageDraw, ImageFont

from argus import ocr
from argus.capture import Shot
from argus.desktop import ArgusError, Paused
from argus.ocr import Line, Word
from argus.testing import wait_for


def line(text, y, x=10, w=12):
    words, cx = [], x
    for t in text.split():
        words.append(Word(t, (cx, y, cx + w * len(t), y + 14)))
        cx += w * len(t) + 8
    return Line(text, tuple(words))


LINES = [line("File Edit View", 5), line("error TS2304: Cannot find name 'foo'", 100), line("Build succeeded", 200), line("Submit", 300)]


def test_find_exact_case_insensitive_and_boxed_to_the_words():
    hits = ocr.find(LINES, "cannot find")
    assert len(hits) == 1 and hits[0].text == "Cannot find" and hits[0].score >= 0.95
    x0, y0, x1, y1 = hits[0].box
    assert y0 == 100 and x0 > 100  # starts at "Cannot", not at the line start


def test_find_tolerates_ocr_mistakes():
    hits = ocr.find([line("Bui1d succeded", 0)], "build succeeded")
    assert hits and hits[0].score < 0.95


def test_find_nothing():
    assert ocr.find(LINES, "deploy failed") == []


def test_as_text_scales_positions():
    assert ocr.as_text([line("hello", 100, x=200)], scale=0.5) == "[100,50] hello"


def _ocr_available() -> bool:
    try:
        ocr._get_engine()
        return True
    except ArgusError:
        return False


@pytest.mark.skipif(not _ocr_available(), reason="no Windows OCR language installed (e.g. some CI images)")
def test_real_windows_ocr_reads_rendered_text():
    img = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(img).text((20, 40), "Compiled successfully in 812ms", fill="black", font=ImageFont.truetype("segoeui.ttf", 30))
    lines = asyncio.run(ocr.read(img))
    assert any("Compiled successfully" in ln.text for ln in lines)


def _frames(*colours):
    shots = [Shot(image=Image.new("RGB", (100, 100), c), kind="region", label="t", origin=(0, 0), target={"region": [0, 0, 100, 100]}) for c in colours]
    it = iter(shots)

    async def grab():
        try:
            return next(it)
        except StopIteration:
            return shots[-1]

    return grab


async def _nosleep(_s):
    await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


def test_wait_for_change():
    r = run(wait_for(_frames("white", "white", "black"), "change", timeout=5, sleep=_nosleep))
    assert r.ok and r.diff.changed == 1.0


def test_wait_for_change_times_out():
    r = run(wait_for(_frames("white"), "change", timeout=0.05, sleep=_nosleep))
    assert not r.ok and "didn't change" in r.detail


def test_wait_for_stable():
    r = run(wait_for(_frames("red", "green", "blue", "blue"), "stable", timeout=5, quiet=0, sleep=_nosleep))
    assert r.ok and r.shot.image.getpixel((0, 0)) == (0, 0, 255)


def test_wait_for_text_appearing_and_going():
    seen = iter([[], [], [line("Done", 0)]])

    async def read(_img):
        return next(seen, [line("Done", 0)])

    r = run(wait_for(_frames("white"), "text", text="done", read=read, find=ocr.find, timeout=5, sleep=_nosleep))
    assert r.ok and r.matches

    gone = iter([[line("Loading", 0)], []])

    async def read2(_img):
        return next(gone, [])

    r = run(wait_for(_frames("white"), "text_gone", text="loading", read=read2, find=ocr.find, timeout=5, sleep=_nosleep))
    assert r.ok


def test_wait_needs_text_and_valid_mode():
    with pytest.raises(ArgusError):
        run(wait_for(_frames("white"), "text", timeout=1, sleep=_nosleep))
    with pytest.raises(ArgusError):
        run(wait_for(_frames("white"), "sideways", timeout=1, sleep=_nosleep))


def test_pause_stops_a_wait():
    calls = {"n": 0}

    async def grab():
        calls["n"] += 1
        if calls["n"] > 1:
            raise Paused("paused")
        return Shot(image=Image.new("RGB", (10, 10)), kind="region", label="t", origin=(0, 0), target={})

    with pytest.raises(Paused):
        run(wait_for(grab, "change", timeout=5, sleep=_nosleep))


def test_target_closing_counts_as_change():
    calls = {"n": 0}

    async def grab():
        calls["n"] += 1
        if calls["n"] > 1:
            raise ArgusError("window gone")
        return Shot(image=Image.new("RGB", (10, 10)), kind="window", label="t", origin=(0, 0), target={})

    r = run(wait_for(grab, "change", timeout=5, sleep=_nosleep))
    assert r.ok and "went away" in r.detail
