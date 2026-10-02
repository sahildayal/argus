import os
import time

from PIL import Image

from argus import snips
from argus.capture import Shot
from argus.imaging import render
from argus.marks import recent_marks, save_mark
from argus.store import Store, open_file


def _png(path, colour, age):
    Image.new("RGB", (200, 100), colour).save(path)
    t = time.time() - age
    os.utime(path, (t, t))


def test_latest_snips_newest_first(tmp_path, monkeypatch):
    _png(tmp_path / "Screenshot 1.png", "red", 300)
    _png(tmp_path / "Screenshot 2.png", "green", 30)
    monkeypatch.setattr(snips, "clipboard_image", lambda: None)
    got = snips.latest(2, folder=tmp_path)
    assert [s.path.name for s in got] == ["Screenshot 2.png", "Screenshot 1.png"]
    assert got[0].source == "snip"


def test_clipboard_copy_of_newest_snip_is_not_duplicated(tmp_path, monkeypatch):
    _png(tmp_path / "Screenshot 2.png", "green", 30)
    monkeypatch.setattr(snips, "clipboard_image", lambda: Image.new("RGB", (200, 100), "green"))
    got = snips.latest(3, folder=tmp_path)
    assert len(got) == 1 and got[0].source == "snip"


def test_newer_clipboard_image_comes_first(tmp_path, monkeypatch):
    _png(tmp_path / "Screenshot 1.png", "red", 600)
    monkeypatch.setattr(snips, "clipboard_image", lambda: Image.new("RGB", (50, 50), "blue"))
    got = snips.latest(2, folder=tmp_path)
    assert [s.source for s in got] == ["clipboard", "snip"]


def test_fresh_snip_beats_clipboard_image_of_unknown_age(tmp_path, monkeypatch):
    _png(tmp_path / "Screenshot 1.png", "red", 10)
    monkeypatch.setattr(snips, "clipboard_image", lambda: Image.new("RGB", (50, 50), "blue"))
    got = snips.latest(2, folder=tmp_path)
    assert [s.source for s in got] == ["snip", "clipboard"]


def _shot(colour="white", kind="monitor"):
    return Shot(image=Image.new("RGB", (320, 200), colour), kind=kind, label="Monitor 2 (center), DELL",
                origin=(2560, 0), target={"monitor": "2"}, cursor=(2600, 50))


def test_store_saves_png_with_metadata_and_reopens_it():
    store = Store(keep=2)
    shot = _shot()
    view, _ = render(shot.image, max_side=2000)
    cap = store.add(shot, view)
    store.flush()
    assert cap.id == "c1" and cap.path.exists() and "monitor-2-center" in cap.path.name
    again = open_file(cap.path)
    assert again.origin == (2560, 0) and again.target == {"monitor": "2"} and again.label == shot.label
    store.add(_shot(), view)
    store.add(_shot(), view)
    assert store.get().id == "c3"
    import pytest

    from argus.desktop import ArgusError

    with pytest.raises(ArgusError, match="Unknown capture id"):
        store.get("c1")  # evicted (keep=2)


def test_marks_round_trip():
    mark = save_mark(_shot("red", "window"), _shot("blue"), extra={"note": "x"})
    latest = recent_marks(1)[0]
    assert latest.id == mark.id and latest.info["note"] == "x"
    assert latest.shot("window").image.getpixel((0, 0)) == (255, 0, 0)
    assert latest.shot("monitor").image.getpixel((0, 0)) == (0, 0, 255)
    assert latest.shot("window").kind == "mark"
