"""The user's own captures: Win+Shift+S / Snipping Tool / PrtScn snips (auto-saved
to Pictures\\Screenshots) and images copied to the clipboard."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageGrab

from . import win32

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}


def screenshots_dir() -> Path:
    path = win32.known_folder(win32.FOLDERID_SCREENSHOTS)
    if path and path.exists():
        return path
    pictures = win32.known_folder(win32.FOLDERID_PICTURES) or Path.home() / "Pictures"
    return pictures / "Screenshots"


def recent_files(limit: int, folder: Path | None = None) -> list[tuple[Path, float]]:
    folder = folder or screenshots_dir()
    if not folder.exists():
        return []
    files = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            try:
                files.append((p, p.stat().st_mtime))
            except OSError:
                continue
    files.sort(key=lambda pm: pm[1], reverse=True)
    return files[:limit]


def clipboard_image() -> Image.Image | None:
    try:
        data = ImageGrab.grabclipboard()
    except Exception:  # clipboard locked by another app, odd formats...
        return None
    if isinstance(data, Image.Image):
        return data.convert("RGB")
    if isinstance(data, list):  # files copied in Explorer
        for name in data:
            p = Path(name)
            if p.suffix.lower() in IMAGE_EXTS and p.is_file():
                try:
                    with Image.open(p) as im:
                        return im.convert("RGB")
                except OSError:
                    continue
    return None


def same_image(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:
        return False
    small = (64, 36)
    diff = ImageChops.difference(a.convert("RGB").resize(small), b.convert("RGB").resize(small))
    return max(hi for _lo, hi in diff.getextrema()) <= 16


@dataclass
class Snip:
    image: Image.Image
    source: str  # "snip" (saved file) | "clipboard"
    path: Path | None
    when: float | None  # unknown for clipboard images copied before Argus started


def latest(count: int = 1, clip_time: float | None = None, clip_seq: int | None = None, folder: Path | None = None) -> list[Snip]:
    """Newest snips first. The clipboard image counts as a snip unless it's the
    same picture as the newest saved one (a Win+Shift+S snip lands in both)."""
    count = max(1, min(count, 10))
    files = recent_files(count, folder)
    entries: list[tuple[float, Snip | tuple[Path, float]]] = [(mtime, (path, mtime)) for path, mtime in files]

    clip = clipboard_image()
    if clip is not None:
        newest = _open(files[0][0]) if files else None
        if newest is None or not same_image(clip, newest):
            known = clip_time if clip_seq is not None and clip_seq == win32.GetClipboardSequenceNumber() else None
            if known is not None:
                rank = known
            elif files and time.time() - files[0][1] < 120:
                rank = files[0][1] - 1  # a fresh snip file beats a clipboard image of unknown age
            else:
                rank = time.time()
            entries.append((rank, Snip(clip, "clipboard", None, known)))

    entries.sort(key=lambda e: e[0], reverse=True)
    out: list[Snip] = []
    for _rank, item in entries:
        if isinstance(item, Snip):
            out.append(item)
        else:
            img = _open(item[0])
            if img is not None:
                out.append(Snip(img, "snip", item[0], item[1]))
        if len(out) >= count:
            break
    return out


def _open(path: Path) -> Image.Image | None:
    try:
        with Image.open(path) as im:
            return im.convert("RGB")
    except OSError:
        return None
