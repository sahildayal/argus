"""Rectangles in desktop coordinates (physical pixels, origin at the primary
monitor's top-left; monitors left of or above it have negative coordinates)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def ltrb(cls, left: int, top: int, right: int, bottom: int) -> Rect:
        return cls(left, top, right - left, bottom - top)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(self.w, 0) * max(self.h, 0)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    def ltrb_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.right, self.bottom)

    def intersect(self, other: Rect) -> Rect | None:
        left, top = max(self.x, other.x), max(self.y, other.y)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect.ltrb(left, top, right, bottom)

    def union(self, other: Rect) -> Rect:
        return Rect.ltrb(
            min(self.x, other.x),
            min(self.y, other.y),
            max(self.right, other.right),
            max(self.bottom, other.bottom),
        )

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def offset(self, dx: int, dy: int) -> Rect:
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def overlap_fraction(self, other: Rect) -> float:
        """Share of *self* covered by *other*."""
        inter = self.intersect(other)
        return inter.area / self.area if inter and self.area else 0.0

    def __str__(self) -> str:
        return f"{self.w}x{self.h} at ({self.x}, {self.y})"


def bounding(rects: list[Rect]) -> Rect | None:
    if not rects:
        return None
    out = rects[0]
    for r in rects[1:]:
        out = out.union(r)
    return out
