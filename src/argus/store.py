"""Recent captures: kept in memory for zoom/compare/OCR, saved to disk as PNGs
with their metadata embedded so a later session can reopen them."""

from __future__ import annotations

import errno
import json
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, PngImagePlugin

from . import seal, win32
from .capture import Shot
from .config import Settings, home
from .text import display_arg
from .desktop import ArgusError
from .errors import work_profile
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


_batch: ContextVar[list[tuple[Store, Capture, bool]] | None] = ContextVar("capture_batch", default=None)


@contextmanager
def defer_captures():
    """Keep new captures private to this call until its disclosure succeeds."""
    token = _batch.set([])
    try:
        yield
    finally:
        _batch.reset(token)


def publish_captures(visible: list[Capture] | None = None) -> None:
    """Publish capture IDs. When gated, only save images shown in the preview."""
    batch = _batch.get()
    if batch is None:
        return
    visible_ids = None if visible is None else {id(cap) for cap in visible}
    for store, cap, save in batch:
        if save and visible_ids is not None and id(cap) not in visible_ids:
            cap.path = None
            save = False
        store._publish(cap, save)
    batch.clear()


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
        path = self._save_path(cid, shot) if save else shot.source
        cap = Capture(cid, shot, view, path)
        batch = _batch.get()
        if batch is not None:
            batch.append((self, cap, save))
        else:
            self._publish(cap, save)
        return cap

    def _publish(self, cap: Capture, save: bool) -> None:
        path = cap.path
        if save and path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if save and path is not None:
                self._pending = [f for f in self._pending if not f.done()]
                self._pending.append(_writer.submit(write_png, cap.image, path, meta(cap.shot)))
            self._items[cap.id] = cap
            while len(self._items) > self.keep:
                self._items.popitem(last=False)

    def _save_path(self, cid: str, shot: Shot) -> Path:
        """Reserve a path for the caption without creating a capture file."""
        stamp = time.localtime(shot.taken)
        folder = home() / "captures" / time.strftime("%Y-%m-%d", stamp)
        return folder / f"{time.strftime('%H%M%S', stamp)}_{self._tag}-{cid}_{_slug(shot.label)}.png"

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
        raise ArgusError(f"Unknown capture id {display_arg(cid)!r}. Recent captures: {recent}. (Older ones can be reopened by file path.)")

    def latest(self) -> Capture | None:
        with self._lock:
            return next(reversed(self._items.values()), None)


# One writer thread keeps saves in order; its pending work finishes before exit.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="argus-save")


def png_info(image: Image.Image, data: dict) -> PngImagePlugin.PngInfo:
    """PNG text chunks holding *data* and the seal that authenticates it."""
    text = json.dumps(data)
    info = PngImagePlugin.PngInfo()
    info.add_text("argus", text)
    info.add_text("argus-seal", seal.sign(text, image))
    return info


def write_png(image: Image.Image, path: Path, data: dict, compress_level: int = 1) -> None:
    """Write atomically, so nobody ever opens a half-written capture."""
    info = png_info(image, data)
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
        "profile": shot.profile,
        "policy": shot.policy,
    }


def work_replay_path(path: str | Path) -> Path:
    """Check local, managed paths before probing or opening an image."""
    refused = "Work profile image paths must be inside Argus's local captures, marks or baselines folders."
    raw = os.fspath(path).replace("/", "\\")
    if raw.startswith("\\\\"):
        raise ArgusError(refused)
    candidate = os.path.abspath(os.path.expanduser(path))
    base = os.path.abspath(home())

    def local(p: str) -> bool:
        drive, tail = os.path.splitdrive(p)
        return bool(re.fullmatch(r"[a-zA-Z]:", drive) and ":" not in tail
                    and win32.GetDriveTypeW(drive + "\\") not in (0, 1, 4))

    def inside(p: str, parent: str) -> bool:
        return p.casefold().startswith(parent.rstrip("\\/").casefold() + os.sep)

    roots = [os.path.join(base, kind) for kind in ("captures", "marks", "baselines")]

    def managed(p: str) -> bool:
        return any(inside(p, root) for root in roots) and local(p)

    # Inspect redirects before realpath can follow them and contact a network
    # share. readlink reads the local junction/symlink itself, not its target.
    for _ in range(40):
        if not managed(candidate):
            raise ArgusError(refused)
        relative = candidate[len(base.rstrip("\\/")) + 1:]
        parts = Path(relative).parts
        prefix = base
        redirected = False
        for i, part in enumerate(parts):
            prefix = os.path.join(prefix, part)
            try:
                destination = os.readlink(prefix)
            except FileNotFoundError:
                break
            except OSError as exc:
                if exc.errno != errno.EINVAL:  # a regular file/directory is not a reparse point
                    raise ArgusError(refused) from None
            else:
                if re.match(r"^\\\\\?\\[a-zA-Z]:\\", destination):
                    destination = destination[4:]  # Windows uses this prefix for absolute local links
                candidate = os.path.abspath(os.path.join(os.path.dirname(prefix), destination, *parts[i + 1:]))
                redirected = True
                break
        if not redirected:
            break
    else:
        raise ArgusError(refused)
    try:
        resolved = os.path.realpath(candidate)
    except (OSError, ValueError):
        raise ArgusError(refused) from None
    if not managed(resolved):
        raise ArgusError(refused)
    return Path(resolved)


def open_file(path: str | Path, *, work: bool | None = None) -> Shot:
    """Reopen a saved capture (or any image file) as a shot."""
    # Work profile: file names never go into error text (capture names are built
    # from window labels, and a user's own files can be named after anything).
    work = work_profile() if work is None else work
    p = work_replay_path(path) if work else Path(path).expanduser()
    if not p.is_file():
        raise ArgusError("No such file (name withheld by the work profile)." if work else f"No such file: {display_arg(str(p))}")
    try:
        with Image.open(p) as im:
            im.load()
            info = dict(getattr(im, "text", {}) or {})
            img = im.convert("RGB")
    except OSError as exc:
        if work:
            raise ArgusError(f"That file can't be opened as an image ({type(exc).__name__}).") from None
        raise ArgusError(f"Can't open {display_arg(p.name)} as an image ({display_arg(str(exc))})") from None
    data: dict = {}
    if "argus" in info:
        try:
            data = json.loads(info["argus"])
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    # Only a file Argus itself wrote (valid seal) keeps its policy stamp; without
    # one the work profile won't show the image again (see `replayable`).
    authentic = bool(data) and seal.verify(info["argus"], img, str(info.get("argus-seal") or ""))
    origin = tuple(data["origin"]) if data.get("origin") else None
    cursor = tuple(data["cursor"]) if data.get("cursor") else None
    return Shot(
        image=img,
        kind="file",
        label=display_arg(data.get("label") or p.name),
        origin=origin,  # type: ignore[arg-type]
        target=data.get("target") or {},
        method="file",
        hidden=list(data.get("hidden") or []),
        cursor=cursor,  # type: ignore[arg-type]
        taken=float(data.get("taken") or p.stat().st_mtime),
        source=p,
        profile=str(data.get("profile") or ""),
        policy=str(data.get("policy") or "") if authentic else "",
        sealed=authentic,
    )


def replayable(shot: Shot, s: Settings) -> bool:
    """May *shot* be shown (again) under settings *s*? Always in the personal
    profile. In the work profile only images Argus redacted under the current
    work policy: not personal-profile captures, not captures from before the
    allow/block lists changed, and not files without a valid seal."""
    return not s.work or (shot.profile == "work" and bool(shot.policy) and shot.policy == s.policy)
