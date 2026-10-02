"""Hotkey marks: the tray app saves what was under the mouse when the user pressed
the mark hotkey; look() and latest_mark() read them back.

Layout: ~/.argus/marks/<id>.json plus <id>.window.png and <id>.monitor.png.
The JSON is written last, so a reader never sees a half-saved mark."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .capture import Shot
from .config import home
from .store import meta, open_file, write_png


def marks_dir() -> Path:
    path = home() / "marks"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Mark:
    id: str
    when: float
    info: dict
    window_png: Path | None
    monitor_png: Path | None

    def shot(self, scope: str = "window") -> Shot | None:
        path = self.monitor_png if scope == "monitor" else (self.window_png or self.monitor_png)
        if path is None or not path.exists():
            return None
        shot = open_file(path)
        shot.kind = "mark"
        return shot


def _save_png(shot: Shot, path: Path) -> None:
    write_png(shot.image, path, meta(shot))


def save_mark(window: Shot | None, monitor: Shot | None, extra: dict | None = None) -> Mark:
    now = time.time()
    mid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    folder = marks_dir()
    files = {}
    if window is not None:
        files["window"] = f"{mid}.window.png"
        _save_png(window, folder / files["window"])
    if monitor is not None:
        files["monitor"] = f"{mid}.monitor.png"
        _save_png(monitor, folder / files["monitor"])
    info = {
        "id": mid,
        "when": now,
        "window": window.label if window else None,
        "monitor": monitor.label if monitor else None,
        "hidden": sorted(set((window.hidden if window else []) + (monitor.hidden if monitor else []))),
        "files": files,
        **(extra or {}),
    }
    tmp = folder / f"{mid}.json.tmp"
    tmp.write_text(json.dumps(info, indent=1), encoding="utf-8")
    os.replace(tmp, folder / f"{mid}.json")
    return _load(folder / f"{mid}.json")  # type: ignore[return-value]


def _load(path: Path) -> Mark | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    folder = path.parent
    files = info.get("files") or {}
    return Mark(
        id=info.get("id", path.stem),
        when=float(info.get("when") or path.stat().st_mtime),
        info=info,
        window_png=folder / files["window"] if files.get("window") else None,
        monitor_png=folder / files["monitor"] if files.get("monitor") else None,
    )


def recent_marks(limit: int = 5) -> list[Mark]:
    paths = sorted(marks_dir().glob("*.json"), reverse=True)  # ids sort by time
    out = []
    for p in paths:
        m = _load(p)
        if m is not None:
            out.append(m)
        if len(out) >= limit:
            break
    return out


def latest_mark() -> Mark | None:
    marks = recent_marks(1)
    return marks[0] if marks else None
