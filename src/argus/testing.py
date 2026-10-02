"""Visual testing: pixel diffs with changed regions, saved baselines, and waiting
for the screen to change, settle, or show some text."""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, PngImagePlugin

from .capture import Shot
from .config import home
from .desktop import ArgusError, Paused
from .geometry import Rect
from .store import meta, open_file

# ---------------------------------------------------------------------- diff


@dataclass
class Diff:
    changed: float  # fraction of pixels that differ
    regions: list[Rect]  # changed areas in the first image's pixels, biggest first
    note: str | None = None

    @property
    def percent(self) -> str:
        p = self.changed * 100
        return f"{p:.2f}%" if p < 10 else f"{p:.0f}%"


def diff(
    a: Image.Image,
    b: Image.Image,
    *,
    threshold: int = 24,
    ignore: list[Rect] | None = None,
    block: int = 8,
    max_regions: int = 12,
) -> Diff:
    """Compare two images. Per-channel differences up to *threshold* (0-255) are
    treated as noise (anti-aliasing, compression)."""
    note = None
    if a.size != b.size:
        note = f"Sizes differ ({a.width}x{a.height} vs {b.width}x{b.height}); the second was scaled to match."
        b = b.resize(a.size, Image.Resampling.BILINEAR)
    delta = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    # Max over channels, so a pure-blue change counts as much as a grey one.
    r, g, bl = delta.split()
    peak = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    mask = peak.point(lambda p: 255 if p > threshold else 0)
    if ignore:
        draw = ImageDraw.Draw(mask)
        for rect in ignore:
            draw.rectangle([rect.x, rect.y, rect.right - 1, rect.bottom - 1], fill=0)
    count = mask.histogram()[255]
    if count == 0:
        return Diff(0.0, [], note)
    return Diff(count / (a.width * a.height), _regions(mask, block, max_regions), note)


def _regions(mask: Image.Image, block: int, limit: int) -> list[Rect]:
    w, h = mask.size
    gw, gh = math.ceil(w / block), math.ceil(h / block)
    coarse = mask.resize((gw, gh), Image.Resampling.BOX).point(lambda p: 255 if p else 0)
    # Connect through a dilated copy (bridges one-cell gaps, e.g. between words),
    # but measure each region only from cells that really changed.
    linked = coarse.filter(ImageFilter.MaxFilter(3)).load()
    real = coarse.load()
    seen = bytearray(gw * gh)
    rects: list[Rect] = []
    for y in range(gh):
        for x in range(gw):
            if seen[y * gw + x] or not real[x, y]:
                continue
            x0, y0, x1, y1 = x, y, x, y
            queue = deque([(x, y)])
            seen[y * gw + x] = 1
            while queue:
                cx, cy = queue.popleft()
                if real[cx, cy]:
                    x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < gw and 0 <= ny < gh and not seen[ny * gw + nx] and linked[nx, ny]:
                        seen[ny * gw + nx] = 1
                        queue.append((nx, ny))
            rects.append(Rect.ltrb(x0 * block, y0 * block, min(w, (x1 + 1) * block), min(h, (y1 + 1) * block)))
    rects = _merge(rects)
    rects.sort(key=lambda r: r.area, reverse=True)
    if len(rects) > limit:
        rest = rects[limit - 1 :]
        merged = rest[0]
        for r in rest[1:]:
            merged = merged.union(r)
        rects = rects[: limit - 1] + [merged]
    return rects


def _merge(rects: list[Rect]) -> list[Rect]:
    """Merge overlapping or touching boxes until none overlap."""
    rects = list(rects)
    changed = True
    while changed:
        changed = False
        out: list[Rect] = []
        for r in rects:
            for i, o in enumerate(out):
                if r.x <= o.right and o.x <= r.right and r.y <= o.bottom and o.y <= r.bottom:
                    out[i] = o.union(r)
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


# ----------------------------------------------------------------- baselines


def baselines_dir() -> Path:
    path = home() / "baselines"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _baseline_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")[:60]
    if not safe:
        raise ArgusError("A baseline needs a name, like 'login-page' or 'dashboard-dark'.")
    return baselines_dir() / f"{safe}.png"


def save_baseline(name: str, shot: Shot) -> Path:
    if not shot.target:
        raise ArgusError("Only monitor, window or region captures can be baselines (they get re-captured to compare).")
    path = _baseline_path(name)
    info = PngImagePlugin.PngInfo()
    info.add_text("argus", json.dumps({**meta(shot), "baseline": name}))
    shot.image.save(path, format="PNG", compress_level=6, pnginfo=info)
    return path


def load_baseline(name: str) -> Shot:
    path = _baseline_path(name)
    if not path.exists():
        names = ", ".join(n for n, *_ in list_baselines()) or "none yet"
        raise ArgusError(f"No baseline named {name!r}. Saved baselines: {names}.")
    shot = open_file(path)
    shot.kind = "baseline"
    return shot


def list_baselines() -> list[tuple[str, Path, float, str]]:
    out = []
    for p in sorted(baselines_dir().glob("*.png")):
        try:
            shot = open_file(p)
            out.append((p.stem, p, shot.taken, shot.label))
        except ArgusError:
            continue
    return out


def delete_baseline(name: str) -> bool:
    path = _baseline_path(name)
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------- wait


@dataclass
class WaitResult:
    ok: bool
    shot: Shot | None
    elapsed: float
    detail: str
    diff: Diff | None = None
    matches: list = field(default_factory=list)


async def wait_for(
    grab: Callable[[], Awaitable[Shot]],
    until: str,
    *,
    text: str | None = None,
    read: Callable[[Image.Image], Awaitable[list]] | None = None,
    find: Callable[[list, str], list] | None = None,
    timeout: float = 30.0,
    interval: float = 0.5,
    min_change: float = 0.0005,
    quiet: float = 1.5,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    progress: Callable[[float, str], Awaitable[None]] | None = None,
) -> WaitResult:
    """Poll *grab* until the condition holds or *timeout* passes.

    until: "change" (differs from the first frame by >= min_change of its pixels,
    then lets it settle), "stable" (no change for *quiet* seconds), "text" /
    "text_gone" (OCR finds / stops finding *text*)."""
    import anyio

    sleep = sleep or anyio.sleep
    if until in ("text", "text_gone") and not text:
        raise ArgusError(f'until="{until}" needs the text to look for.')
    if until not in ("change", "stable", "text", "text_gone"):
        raise ArgusError('until must be "change", "stable", "text" or "text_gone".')
    start = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - start

    try:
        first = await grab()
    except ArgusError as exc:
        raise ArgusError(f"Can't start waiting: {exc}") from None
    last = first
    previous = first
    quiet_since: float | None = None
    polls = 0

    while True:
        if until in ("text", "text_gone"):
            lines = await read(last.image)  # type: ignore[misc]
            hits = find(lines, text)  # type: ignore[misc]
            if until == "text" and hits:
                return WaitResult(True, last, elapsed(), f'"{text}" appeared', matches=hits)
            if until == "text_gone" and not hits:
                return WaitResult(True, last, elapsed(), f'"{text}" is no longer on screen')
        elif until == "change" and polls:
            d = diff(first.image, last.image)
            if d.changed >= min_change:
                last = await _settle(grab, last, sleep, min_change)
                return WaitResult(True, last, elapsed(), f"the screen changed ({diff(first.image, last.image).percent} of pixels)", diff(first.image, last.image))
        elif until == "stable" and polls:
            d = diff(previous.image, last.image)
            now = time.monotonic()
            if d.changed < min_change:
                quiet_since = quiet_since or now
                if now - quiet_since >= quiet:
                    return WaitResult(True, last, elapsed(), f"nothing changed for {quiet:g}s")
            else:
                quiet_since = None

        if elapsed() >= timeout:
            what = {
                "change": "the screen didn't change",
                "stable": "the screen kept changing",
                "text": f'"{text}" never appeared',
                "text_gone": f'"{text}" was still on screen',
            }[until]
            return WaitResult(False, last, elapsed(), f"{what} within {timeout:g}s")
        if progress:
            await progress(elapsed(), f"waiting for {until}...")
        await sleep(interval if until in ("change", "stable") else max(interval, 1.0))
        previous = last
        try:
            last = await grab()
        except Paused:
            raise
        except ArgusError as exc:
            gone = f"the target went away ({exc})"
            return WaitResult(until in ("change", "text_gone"), previous, elapsed(), gone)
        polls += 1


async def _settle(grab, shot: Shot, sleep, min_change: float, settle: float = 0.6, limit: float = 5.0) -> Shot:
    """After a change, wait until consecutive frames agree (animations, repaints)."""
    start = time.monotonic()
    current = shot
    calm_since = time.monotonic()
    while time.monotonic() - start < limit:
        await sleep(0.2)
        try:
            nxt = await grab()
        except Paused:
            raise
        except ArgusError:
            return current
        if diff(current.image, nxt.image).changed >= min_change:
            calm_since = time.monotonic()
        current = nxt
        if time.monotonic() - calm_since >= settle:
            break
    return current
