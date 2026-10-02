"""Turning full-resolution captures into the image the AI actually sees:
resizing, the labeled grid, the mouse marker, change boxes, and encoding."""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

# PNG keeps text crisp; past this size a high-quality JPEG is sent instead.
PNG_BUDGET = 1_200_000

MARK = (255, 0, 200)  # grid + mouse marker colour: magenta is rare in real UIs


@lru_cache(maxsize=16)
def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("consola.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def column_name(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        name = chr(65 + rem) + name
    return name


def column_index(name: str) -> int:
    n = 0
    for ch in name.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


@dataclass(frozen=True)
class Grid:
    cell: float  # square cell size, in view pixels
    cols: int
    rows: int

    def cells(self, spec: str) -> tuple[float, float, float, float]:
        """'D7' or 'C3:E5' -> (x0, y0, x1, y1) in view pixels."""
        parts = [p.strip() for p in spec.split(":")]
        if not 1 <= len(parts) <= 2:
            raise ValueError(f"bad cell {spec!r}")
        boxes = []
        for part in parts:
            m = re.fullmatch(r"([A-Za-z]{1,2})\s*(\d{1,3})", part)
            if not m:
                raise ValueError(f'bad cell {part!r} - use a column letter and row number, like "D7"')
            col, row = column_index(m.group(1)), int(m.group(2)) - 1
            if not (0 <= col < self.cols and 0 <= row < self.rows):
                last = f"{column_name(self.cols - 1)}{self.rows}"
                raise ValueError(f"cell {part.upper()} is outside the grid (A1 to {last})")
            boxes.append((col * self.cell, row * self.cell, (col + 1) * self.cell, (row + 1) * self.cell))
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        return (x0, y0, x1, y1)

    def cell_at(self, x: float, y: float) -> str:
        col = min(self.cols - 1, max(0, int(x // self.cell)))
        row = min(self.rows - 1, max(0, int(y // self.cell)))
        return f"{column_name(col)}{row + 1}"

    def describe(self) -> str:
        return f"{self.cols} columns (A-{column_name(self.cols - 1)}) x {self.rows} rows (1-{self.rows})"


@dataclass(frozen=True)
class View:
    """The encoded image the AI sees, and how it maps onto the full capture."""

    width: int
    height: int
    scale: float  # view pixels per full-resolution pixel
    grid: Grid | None
    data: bytes
    mime: str  # "png" or "jpeg"

    def to_full(self, x: float, y: float) -> tuple[float, float]:
        return (x / self.scale, y / self.scale)


def fit(width: int, height: int, max_side: int) -> tuple[int, int, float]:
    s = min(1.0, max_side / max(width, height))
    return max(1, round(width * s)), max(1, round(height * s)), s


def render(
    img: Image.Image,
    *,
    max_side: int,
    grid: bool = False,
    cursor: tuple[float, float] | None = None,
    boxes: list[tuple[float, float, float, float]] | None = None,
    upscale_to: int = 0,
) -> tuple[View, Image.Image]:
    """Resize *img* for the AI and draw overlays. ``cursor`` and ``boxes`` are in
    full-resolution pixel coordinates of *img*. Small crops are enlarged up to
    ``upscale_to`` pixels on the long side so small text stays legible."""
    w, h, s = fit(img.width, img.height, max_side)
    if s < 1:
        # reducing_gap box-filters most of the way first: ~3x faster, same look
        view = img.resize((w, h), Image.Resampling.LANCZOS, reducing_gap=3.0)
    elif upscale_to and max(img.size) < upscale_to:
        s = min(3.0, upscale_to / max(img.size))
        w, h = round(img.width * s), round(img.height * s)
        view = img.resize((w, h), Image.Resampling.LANCZOS)
    else:
        view = img.copy()
    if view.mode != "RGB":
        view = view.convert("RGB")
    if boxes:
        draw_boxes(view, [(x0 * s, y0 * s, x1 * s, y1 * s) for x0, y0, x1, y1 in boxes])
    g = None
    if grid:
        view, g = draw_grid(view)
    if cursor is not None and 0 <= cursor[0] < img.width and 0 <= cursor[1] < img.height:
        draw_cursor(view, cursor[0] * s, cursor[1] * s)
    data, mime = encode(view)
    return View(view.width, view.height, s, g, data, mime), view


def encode(img: Image.Image) -> tuple[bytes, str]:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    if buf.tell() <= PNG_BUDGET:
        return buf.getvalue(), "png"
    buf = io.BytesIO()
    # No optimize=True: on large noisy frames libjpeg's optimizer overflows Pillow's buffer.
    img.convert("RGB").save(buf, format="JPEG", quality=88, subsampling=0)
    return buf.getvalue(), "jpeg"


def draw_grid(img: Image.Image) -> tuple[Image.Image, Grid]:
    w, h = img.size
    cell = float(max(40, min(200, round(max(w, h) / 12))))
    g = Grid(cell, math.ceil(w / cell), math.ceil(h / cell))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for i in range(1, g.cols):
        x = round(i * cell)
        draw.line([(x, 0), (x, h)], fill=(0, 0, 0, 90), width=3)
        draw.line([(x, 0), (x, h)], fill=(*MARK, 190), width=1)
    for j in range(1, g.rows):
        y = round(j * cell)
        draw.line([(0, y), (w, y)], fill=(0, 0, 0, 90), width=3)
        draw.line([(0, y), (w, y)], fill=(*MARK, 190), width=1)
    font = load_font(max(10, min(14, int(cell // 6))))
    for j in range(g.rows):
        for i in range(g.cols):
            label = f"{column_name(i)}{j + 1}"
            x, y = round(i * cell) + 2, round(j * cell) + 2
            tw = draw.textlength(label, font=font)
            draw.rectangle([x, y, x + tw + 4, y + font.size + 3], fill=(0, 0, 0, 150))
            draw.text((x + 2, y), label, fill=(255, 255, 255, 235), font=font)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), g


def draw_cursor(img: Image.Image, x: float, y: float) -> None:
    draw = ImageDraw.Draw(img)
    for radius, width, colour in ((15, 5, (255, 255, 255)), (14, 3, MARK)):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=colour, width=width)
    draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=MARK)


def draw_boxes(img: Image.Image, boxes: list[tuple[float, float, float, float]], colour=(255, 32, 32)) -> None:
    draw = ImageDraw.Draw(img)
    font = load_font(14)
    for n, (x0, y0, x1, y1) in enumerate(boxes, 1):
        draw.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=(255, 255, 255), width=4)
        draw.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=colour, width=2)
        tag = str(n)
        tw = draw.textlength(tag, font=font)
        ty = y0 - 20 if y0 >= 20 else y1 + 3
        draw.rectangle([x0 - 2, ty, x0 + tw + 6, ty + 18], fill=colour)
        draw.text((x0 + 2, ty + 1), tag, fill=(255, 255, 255), font=font)


def side_by_side(left: Image.Image, right: Image.Image, labels=("before", "after")) -> Image.Image:
    gap, bar = 12, 26
    height = max(left.height, right.height)
    out = Image.new("RGB", (left.width + right.width + gap, height + bar), (40, 40, 44))
    out.paste(left, (0, bar))
    out.paste(right, (left.width + gap, bar))
    draw = ImageDraw.Draw(out)
    font = load_font(16)
    draw.text((6, 4), labels[0], fill=(230, 230, 230), font=font)
    draw.text((left.width + gap + 6, 4), labels[1], fill=(230, 230, 230), font=font)
    return out
