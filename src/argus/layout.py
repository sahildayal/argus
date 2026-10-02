"""How people name monitors: numbered left to right, with position words
("left", "center", "right", "bottom" ...) derived from the physical layout."""

from __future__ import annotations

from .geometry import Rect

_ROW_WORDS = {2: ["top", "bottom"], 3: ["top", "middle", "bottom"]}
_COL_WORDS = {
    1: [""],
    2: ["left", "right"],
    3: ["left", "center", "right"],
    4: ["left", "center-left", "center-right", "right"],
}


def _rows(rects: list[Rect]) -> list[list[int]]:
    """Group monitor indices into rows: monitors whose vertical spans overlap by at
    least a third of the shorter one sit side by side."""
    order = sorted(range(len(rects)), key=lambda i: rects[i].center[1])
    rows: list[list[int]] = []
    for i in order:
        r = rects[i]
        for row in rows:
            top = min(rects[j].y for j in row)
            bottom = max(rects[j].bottom for j in row)
            shortest = min([r.h] + [rects[j].h for j in row])
            if min(r.bottom, bottom) - max(r.y, top) >= shortest / 3:
                row.append(i)
                break
        else:
            rows.append([i])
    for row in rows:
        row.sort(key=lambda j: rects[j].center[0])
    rows.sort(key=lambda row: sum(rects[j].center[1] for j in row) / len(row))
    return rows


def _col_words(n: int) -> list[str]:
    if n in _COL_WORDS:
        return _COL_WORDS[n]
    words = ["left"] + [f"{_ordinal(k)} from left" for k in range(2, n)] + ["right"]
    return words


def _ordinal(k: int) -> str:
    return f"{k}{'th' if 11 <= k % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(k % 10, 'th')}"


def arrange(rects: list[Rect], primary: int = 0) -> tuple[list[int], list[str]]:
    """Return (order, labels): ``order`` lists monitor indices in display-number
    order (main row left to right, then the other rows top to bottom), and
    ``labels[i]`` is the position word for monitor ``i``."""
    if not rects:
        return [], []
    if len(rects) == 1:
        return [0], [""]
    rows = _rows(rects)
    main = max(rows, key=lambda row: (len(row), primary in row))

    col = [""] * len(rects)
    for row in rows:
        for word, j in zip(_col_words(len(row)), row):
            col[j] = word

    row_word = [""] * len(rects)
    if len(rows) > 1:
        words = _ROW_WORDS.get(len(rows)) or [f"row {k + 1}" for k in range(len(rows))]
        for word, row in zip(words, rows):
            for j in row:
                row_word[j] = word

    labels = []
    for i in range(len(rects)):
        clash = any(col[i] == col[j] for j in range(len(rects)) if j != i)
        if not col[i]:
            labels.append(row_word[i])  # alone in its row: "top" / "bottom"
        elif clash and row_word[i]:
            labels.append(f"{row_word[i]}-{col[i]}")
        else:
            labels.append(col[i])

    order = list(main) + [j for row in rows if row is not main for j in row]
    return order, labels
