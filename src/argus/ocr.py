"""Reading text off captures with the OCR engine built into Windows (offline)."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from PIL import Image

from .desktop import ArgusError

# Windows OCR reads screen text far better once glyphs are ~20px+ tall, so small
# captures are enlarged first (coordinates are mapped back afterwards).
_TARGET_SIDE = 3200


@dataclass(frozen=True)
class Word:
    text: str
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in image pixels


@dataclass(frozen=True)
class Line:
    text: str
    words: tuple[Word, ...]

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (
            min(w.box[0] for w in self.words),
            min(w.box[1] for w in self.words),
            max(w.box[2] for w in self.words),
            max(w.box[3] for w in self.words),
        )


_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from winrt.windows.media.ocr import OcrEngine
        except ImportError as exc:  # pragma: no cover
            raise ArgusError(f"Windows OCR isn't available ({exc}).") from None
        _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise ArgusError("Windows has no OCR language installed (Settings > Time & language > Language).")
    return _engine


async def read(img: Image.Image) -> list[Line]:
    """OCR an image; returns lines top to bottom with boxes in *img* pixels."""
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import Buffer

    engine = _get_engine()
    limit = OcrEngine.max_image_dimension
    factor = 1.0
    longest = max(img.size)
    if longest < _TARGET_SIDE / 1.5:
        factor = min(3.0, _TARGET_SIDE / longest, limit / longest)
    elif longest > limit:
        factor = limit / longest
    work = img if factor == 1.0 else img.resize((max(1, round(img.width * factor)), max(1, round(img.height * factor))), Image.Resampling.LANCZOS)

    bgra = work.convert("RGBA").tobytes("raw", "BGRA")
    buf = Buffer(len(bgra))
    buf.length = len(bgra)
    memoryview(buf)[:] = bgra
    bitmap = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.BGRA8, work.width, work.height)
    result = await engine.recognize_async(bitmap)

    lines = []
    for line in result.lines:
        words = []
        for w in line.words:
            r = w.bounding_rect
            words.append(Word(w.text, (r.x / factor, r.y / factor, (r.x + r.width) / factor, (r.y + r.height) / factor)))
        if words:
            lines.append(Line(line.text, tuple(words)))
    lines.sort(key=lambda ln: (round(ln.box[1] / 8), ln.box[0]))
    return lines


def as_text(lines: list[Line], with_positions: bool = True, scale: float = 1.0) -> str:
    """One line per OCR line; positions are scaled into the coordinates the AI saw."""
    if not lines:
        return "(no text found)"
    if not with_positions:
        return "\n".join(ln.text for ln in lines)
    return "\n".join(f"[{round(ln.box[0] * scale)},{round(ln.box[1] * scale)}] {ln.text}" for ln in lines)


@dataclass(frozen=True)
class Match:
    text: str  # the matched words as OCR read them
    line: str
    box: tuple[float, float, float, float]  # image pixels
    score: float  # 1.0 = exact (case-insensitive)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def find(lines: list[Line], query: str, fuzzy: float = 0.8) -> list[Match]:
    """Matches of *query* (case-insensitive; tolerant of small OCR errors),
    best first, each boxed to just the words that matched."""
    q = _norm(query)
    if not q:
        raise ArgusError("text to find is empty")
    q_words = q.split(" ")
    n = len(q_words)
    found: list[Match] = []
    for ln in lines:
        words = ln.words
        for i in range(len(words)):
            for span in {n, n + 1, max(1, n - 1)}:
                chunk = words[i : i + span]
                if len(chunk) < span:
                    continue
                text = " ".join(w.text for w in chunk)
                t = _norm(text)
                if q in t:
                    score = 1.0 if len(t) <= len(q) + 2 else 0.95
                else:
                    score = difflib.SequenceMatcher(None, q, t).ratio()
                if score >= fuzzy:
                    box = (
                        min(w.box[0] for w in chunk),
                        min(w.box[1] for w in chunk),
                        max(w.box[2] for w in chunk),
                        max(w.box[3] for w in chunk),
                    )
                    found.append(Match(text, ln.text, box, round(score, 3)))
    # Keep the best match per location.
    found.sort(key=lambda m: (-m.score, m.box[1], m.box[0]))
    unique: list[Match] = []
    for m in found:
        if not any(_overlaps(m.box, u.box) for u in unique):
            unique.append(m)
    return unique


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])
