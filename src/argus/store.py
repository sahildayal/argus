"""Recent captures: kept in memory for zoom/compare/OCR, saved to disk as PNGs
with their metadata embedded so a later session can reopen them."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, PngImagePlugin

from .capture import Shot
from .config import home
from .desktop import ArgusError
from .imaging import View


@dataclass
class Capture:
    id: str
    shot: Shot
    view: View
    path: Path | None

    @property
    def image(self) -> Image.Image:
        return self.shot.image


def _slug(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:40].rstrip("-") or "capture"


class Store:
    def __init__(self, keep: int = 40):
        self.keep = keep
        self._items: OrderedDict[str, Capture] = OrderedDict()
        self._counter = 0
        self._lock = threading.Lock()
        self._tag = f"{os.getpid() % 100000:05d}"
        self._pending: list[Future] = []

    def add(self, shot: Shot, view: View, save: bool = True) -> Capture:
        with self._lock:
            self._counter += 1
            cid = f"c{self._counter}"
        path = self._save(cid, shot) if save else shot.source
        cap = Capture(cid, shot, view, path)
        with self._lock:
            self._items[cid] = cap
            while len(self._items) > self.keep:
                self._items.popitem(last=False)
        return cap

    def _save(self, cid: str, shot: Shot) -> Path:
        """Pick the file name now; write the PNG in the background so the AI gets
        its answer without waiting on disk (writes land well under a second later)."""
        stamp = time.localtime(shot.taken)
        folder = home() / "captures" / time.strftime("%Y-%m-%d", stamp)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.strftime('%H%M%S', stamp)}_{self._tag}-{cid}_{_slug(shot.label)}.png"
        with self._lock:
            self._pending = [f for f in self._pending if not f.done()]
            self._pending.append(_writer.submit(write_png, shot.image, path, meta(shot)))
        return path

    def flush(self, timeout: float = 30) -> None:
        with self._lock:
            pending = list(self._pending)
        for future in pending:
            future.result(timeout)

    def get(self, cid: str | None = None) -> Capture:
        with self._lock:
            if not self._items:
                raise ArgusError("No captures yet in this session - take a screenshot first.")
            if not cid:
                return next(reversed(self._items.values()))
            key = cid.strip().lower()
            if key in self._items:
                return self._items[key]
            recent = ", ".join(list(self._items)[-8:])
        raise ArgusError(f"Unknown capture id {cid!r}. Recent captures: {recent}. (Older ones can be reopened by file path.)")

    def latest(self) -> Capture | None:
        with self._lock:
            return next(reversed(self._items.values()), None)


# One writer thread keeps saves in order; its pending work finishes before exit.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="argus-save")


def write_png(image: Image.Image, path: Path, data: dict, compress_level: int = 1) -> None:
    """Write atomically, so nobody ever opens a half-written capture."""
    info = PngImagePlugin.PngInfo()
    info.add_text("argus", json.dumps(data))
    tmp = path.with_name(path.name + ".part")
    image.save(tmp, format="PNG", compress_level=compress_level, pnginfo=info)
    os.replace(tmp, path)


def meta(shot: Shot) -> dict:
    return {
        "kind": shot.kind,
        "label": shot.label,
        "origin": shot.origin,
        "target": shot.target,
        "taken": shot.taken,
        "cursor": shot.cursor,
        "hidden": shot.hidden,
    }


def open_file(path: str | Path) -> Shot:
    """Reopen a saved capture (or any image file) as a shot."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ArgusError(f"No such file: {p}")
    try:
        with Image.open(p) as im:
            im.load()
            info = dict(getattr(im, "text", {}) or {})
            img = im.convert("RGB")
    except OSError as exc:
        raise ArgusError(f"Can't open {p.name} as an image ({exc})") from None
    data: dict = {}
    if "argus" in info:
        try:
            data = json.loads(info["argus"])
        except ValueError:
            data = {}
    origin = tuple(data["origin"]) if data.get("origin") else None
    cursor = tuple(data["cursor"]) if data.get("cursor") else None
    return Shot(
        image=img,
        kind="file",
        label=data.get("label") or p.name,
        origin=origin,  # type: ignore[arg-type]
        target=data.get("target") or {},
        method="file",
        hidden=list(data.get("hidden") or []),
        cursor=cursor,  # type: ignore[arg-type]
        taken=float(data.get("taken") or p.stat().st_mtime),
        source=p,
    )
