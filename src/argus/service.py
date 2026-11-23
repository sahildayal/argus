"""Everything Argus can do, independent of how it's called. The MCP server and
the CLI are thin wrappers that turn a Result into MCP content or terminal output."""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio

from . import ocr, privacy, testing
from .capture import Shot, capture, capture_monitor, capture_window, recapture
from .config import Settings, config_path, home, pretty_hotkey, settings
from .desktop import ArgusError, Desktop, Host, PolicyChanged, Window, detect_host, snapshot
from .focus import Tracker
from .geometry import Rect
from .imaging import render, side_by_side
from .looking import ago, choose, host_label
from .marks import recent_marks
from .snips import latest as latest_snips, screenshots_dir
from .store import Capture, Store, open_file, replayable


@dataclass
class Result:
    parts: list[str | Capture] = field(default_factory=list)
    sources: list[Capture] = field(default_factory=list)
    evidence: list[Shot] = field(default_factory=list)
    mark_id: str | None = None
    screen_derived: bool = True
    window_scope: privacy.WindowSet | None = None

    def text(self, *lines: str) -> Result:
        self.parts.append("\n".join(line for line in lines if line))
        return self

    def add(self, cap: Capture) -> Result:
        self.parts.append(cap)
        return self

    @property
    def captures(self) -> list[Capture]:
        return [p for p in self.parts if isinstance(p, Capture)]


def caption(cap: Capture) -> str:
    shot, view = cap.shot, cap.view
    w, h = shot.image.size
    lines = [f"[{cap.id}] {shot.label}"]
    if view.scale < 0.999:
        lines.append(
            f"Size: {w}x{h}, shown at {view.width}x{view.height} ({view.scale:.0%}). "
            f'zoom(capture_id="{cap.id}", box=[x1, y1, x2, y2]) shows any part at full resolution.'
        )
    elif view.scale > 1.001:
        lines.append(f"Size: {w}x{h}, enlarged {view.scale:.1f}x to {view.width}x{view.height}.")
    else:
        lines.append(f"Size: {w}x{h}, full resolution.")
    cursor = shot.cursor_in_image
    if cursor is not None and settings().cursor_marker:
        lines.append(f"Mouse pointer: ({round(cursor[0] * view.scale)}, {round(cursor[1] * view.scale)}) in this image, ringed in magenta.")
    if view.grid is not None:
        lines.append(f'Grid: {view.grid.describe()}. zoom(capture_id="{cap.id}", cell="C4") or a range like "B2:D5".')
    if shot.origin is not None and shot.kind in ("window", "region", "zoom"):
        lines.append(f"Desktop position of the top-left pixel: ({shot.origin[0]}, {shot.origin[1]}).")
    if shot.hidden:
        lines.append("Blacked out by the user's privacy list: " + ", ".join(sorted(set(shot.hidden))) + ".")
    lines.extend(shot.notes)
    if cap.path is not None:
        lines.append(f"File: {cap.path}")
    return "\n".join(lines)


def _looks_like_path(ref: str) -> bool:
    return any(c in ref for c in ":\\/") or ref.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".webp"))


def _profile_ok(shot: Shot, s: Settings) -> bool:
    """In the work profile, only images Argus redacted under the current work
    policy may be shown again (see store.replayable)."""
    return replayable(shot, s)


def _require_profile(shot: Shot, s: Settings) -> None:
    if not _profile_ok(shot, s):
        raise ArgusError(
            "The user's work profile is on, and that image wasn't taken under its current settings (it's from the "
            "personal profile, from before the allowed apps changed, or isn't a capture Argus can vouch for), so Argus "
            "won't show it. Take a fresh capture instead."
        )


def _same_policy(s: Settings) -> None:
    """End a multi-frame call if the profile or its allow/block lists changed
    since it started: frames redacted under the old rules must not go out.
    Only matters when the work profile is involved, before or after."""
    now = settings()
    if now.policy != s.policy and (s.work or now.work):
        raise PolicyChanged(
            "The user changed Argus's privacy profile or its allowed apps while this was running, so Argus stopped and "
            "discarded what it had captured. Start again if it's still needed."
        )


class Session:
    def __init__(self, host: Host | None = None, track: bool = True, *, defer_marks: bool = False):
        self.host = host or detect_host()
        self.tracker = Tracker().start() if track else None
        self.store = Store()
        self.used_marks: set[str] = set()
        self.defer_marks = defer_marks

    # -- plumbing ----------------------------------------------------------------

    def desk(self) -> Desktop:
        return snapshot(self.host)

    def history(self) -> list[tuple[float, int]]:
        return self.tracker.recent() if self.tracker else []

    def guard(self) -> Settings:
        privacy.ensure_not_paused()
        s = settings()
        privacy.cleanup(s)
        return s

    def register(
        self,
        shot: Shot,
        *,
        grid: bool | None = None,
        max_side: int | None = None,
        boxes: list[tuple[float, float, float, float]] | None = None,
        upscale_to: int = 0,
        save: bool | None = None,
    ) -> Capture:
        s = settings()
        cursor = shot.cursor_in_image if s.cursor_marker else None
        view, _ = render(
            shot.image,
            max_side=max_side or s.max_side,
            grid=s.grid if grid is None else grid,
            cursor=cursor,
            boxes=boxes,
            upscale_to=upscale_to,
        )
        return self.store.add(shot, view, save=shot.source is None if save is None else save)

    def _show(self, result: Result, shots: list[Shot], grid: bool | None = None, **kw) -> Result:
        multi = len(shots) > 1
        for shot in shots:
            result.add(self.register(shot, grid=grid, max_side=settings().max_side_multi if multi else None, **kw))
        return result

    def capture_ref(self, ref: str | None) -> Capture:
        s = settings()
        if ref and _looks_like_path(ref):
            shot = open_file(ref.strip().strip('"'))
            _require_profile(shot, s)
            return self.register(shot, save=False)
        cap = self.store.get(ref)
        _require_profile(cap.shot, s)
        return cap

    # -- look --------------------------------------------------------------------

    def look(self, scope: str = "window", grid: bool | None = None, fresh: bool = False) -> Result:
        s = self.guard()
        if scope not in ("window", "monitor"):
            raise ArgusError('scope must be "window" or "monitor"')
        if not fresh:
            marks = recent_marks(1)
            mark = marks[0] if marks else None
            if mark and mark.id not in self.used_marks and time.time() - mark.when <= s.mark_freshness:
                shot = mark.shot(scope)
                if shot is not None and _profile_ok(shot, s):
                    if not self.defer_marks:
                        self.used_marks.add(mark.id)
                    what = mark.info.get("monitor" if scope == "monitor" else "window") or mark.info.get("monitor")
                    result = Result(mark_id=mark.id).text(
                        f"Using the hotkey mark you made {ago(time.time() - mark.when)} ago: {what}. "
                        "(look(fresh=true) captures live instead.)"
                    )
                    return self._show(result, [shot], grid)

        desk = self.desk()

        def name(w: Window) -> str:
            return privacy.display_name(w, s)

        history = self.history()
        # Work profile: only windows it actually shows (allowed and not on the
        # block list) can be picked or offered as alternatives.
        choice = choose(desk, history, (lambda w: privacy.shown(w, s)) if s.work else None)
        footer = []
        notice = ""
        if s.work:
            likely = choose(desk, history).window
            if likely is not None and not privacy.shown(likely, s):
                notice = (
                    f"The window the user was most likely looking at, {name(likely)}, isn't allowed by their work "
                    "profile, so Argus won't show it."
                )
        if choice.host is not None:
            footer.append(f"Skipped {name(choice.host)}: that's the window you're chatting with me in.")
        if choice.alternatives:
            alts = "; ".join(
                f"{why}: {name(w)}" + (f" on Monitor {w.monitor}" if w.monitor else "") + f" [{w.handle}]"
                for why, w in choice.alternatives
            )
            footer.append(f"Other candidates - {alts}. If I picked the wrong one, use screenshot(window=\"<handle>\").")

        if choice.window is None and s.work:
            raise ArgusError(
                " ".join(
                    filter(None, [notice, "No window the work profile allows is open (other than this chat). Tell the "
                                  "user; only they can allow apps (under [work] in ~/.argus/config.toml)."] + footer)
                )
            )
        if choice.window is None:
            mon = desk.cursor_monitor
            result = Result().text(f"No other app window is open, so here is the monitor the mouse is on ({mon.title}).")
            self._show(result, [capture_monitor(desk, mon, s)], grid)
            return result.text(*footer)

        win = choice.window
        if scope == "monitor":
            mon = desk.monitor(win.monitor) if win.monitor else desk.cursor_monitor
            shot = capture_monitor(desk, mon, s)
            header = f"{mon.title}, the monitor showing {name(win)}. Picked because {choice.reason}."
        else:
            try:
                shot = capture_window(desk, win, s)
            except ArgusError as exc:
                raise ArgusError(f"{exc} {' '.join(footer)}") from None
            header = f"Picked {name(win)} because {choice.reason}."
        result = Result().text(notice, header)
        extra = [w for _, w in choice.alternatives]
        if choice.host is not None:
            extra.append(choice.host)
        if shot.share_windows is not None:
            shot.share_windows |= privacy.share_scope(desk, s, extra)
        self._show(result, [shot], grid)
        return result.text(*footer)

    # -- screenshot / zoom -----------------------------------------------------------

    def screenshot(self, monitor=None, window=None, region=None, grid: bool | None = None) -> Result:
        s = self.guard()
        shots = capture(self.desk(), s, monitor, window, region)
        result = Result()
        if len(shots) > 1:
            result.text(f"{len(shots)} monitors, numbered left to right. Each image has its own capture id.")
        return self._show(result, shots, grid)

    def zoom(self, box=None, cell: str | None = None, capture_id: str | None = None, grid: bool | None = None) -> Result:
        self.guard()
        cap = self.capture_ref(capture_id)
        if cell:
            if cap.view.grid is None:
                raise ArgusError(f'{cap.id} has no grid. Re-take it with grid=true, or pass box=[x1, y1, x2, y2].')
            try:
                vx0, vy0, vx1, vy1 = cap.view.grid.cells(cell)
            except ValueError as exc:
                raise ArgusError(str(exc)) from None
        elif box is not None:
            try:
                vx0, vy0, vx1, vy1 = (float(v) for v in box)
            except (TypeError, ValueError):
                raise ArgusError("box must be four numbers: [x1, y1, x2, y2]") from None
        else:
            raise ArgusError('Pass box=[x1, y1, x2, y2] (in the pixels of the image you saw) or cell="D7".')
        vx0, vx1 = sorted((vx0, vx1))
        vy0, vy1 = sorted((vy0, vy1))
        sc = cap.view.scale
        img = cap.image
        x0 = max(0, min(img.width, math.floor(vx0 / sc)))
        y0 = max(0, min(img.height, math.floor(vy0 / sc)))
        x1 = max(0, min(img.width, math.ceil(vx1 / sc)))
        y1 = max(0, min(img.height, math.ceil(vy1 / sc)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise ArgusError(f"That box is outside the image or too small. {cap.id} is {cap.view.width}x{cap.view.height} as you saw it.")
        origin = (cap.shot.origin[0] + x0, cap.shot.origin[1] + y0) if cap.shot.origin else None
        shot = Shot(
            image=img.crop((x0, y0, x1, y1)),
            kind="zoom",
            label=f"zoom into {cap.id} ({cap.shot.label}), full-res pixels [{x0},{y0}]-[{x1},{y1}]",
            origin=origin,
            target={"region": [origin[0], origin[1], x1 - x0, y1 - y0]} if origin else {},
            method="crop",
            hidden=list(cap.shot.hidden),
            cursor=cap.shot.cursor,
            monitor=cap.shot.monitor,
            window=cap.shot.window,
            taken=cap.shot.taken,
            profile=cap.shot.profile,
            share_windows=cap.shot.share_windows,
            policy=cap.shot.policy,
        )
        return Result(sources=[cap]).add(self.register(shot, grid=grid, upscale_to=1200))

    # -- list --------------------------------------------------------------------------

    def list_screens(self) -> Result:
        state = privacy.pause_state()
        from . import __version__

        if state.paused:
            return Result(screen_derived=False).text(f"Argus {__version__}: {state.describe()} by the user. Nothing can be listed or captured until they resume it.")
        s = settings()
        desk = self.desk()
        lines = [f"Argus {__version__}: captures {state.describe()}, {s.profile} profile. You're chatting from: {host_label(desk)}."]
        if s.work:
            lines.append(
                "Work profile: only the apps the user allowed are visible. Everything else (other windows, the desktop, "
                "the taskbar) is blacked out in captures, and hidden windows are listed without their titles."
            )
        cm = desk.cursor_monitor
        lines.append(f"Mouse at ({desk.cursor[0]}, {desk.cursor[1]}), on {cm.title}.")
        lines.append("")
        lines.append("Monitors (numbered left to right):")
        for m in desk.monitors:
            lines.append("  " + m.describe())
        lines.append("")
        lines.append("Windows, front to back (handles work as screenshot(window=...)):")
        for m in desk.monitors:
            wins = [w for w in desk.windows if w.monitor == m.index]
            if wins:
                lines.append(f"  {m.title}:")
                lines.extend("    " + self._window_line(w, desk, s) for w in wins)
        minimized = [w for w in desk.windows if w.minimized]
        if minimized:
            lines.append("  Minimized: " + "; ".join(f"{self._safe_label(w, s)} [{w.handle}]" for w in minimized))
        recent = []
        for t, hwnd in self.history()[:8]:
            w = desk.window(hwnd)
            if w is not None and w.is_app:
                recent.append(f"{self._safe_label(w, s)} ({ago(desk.taken - t)} ago)")
        if recent:
            lines.append("")
            lines.append("Recently focused, newest first: " + "; ".join(recent[:6]) + ".")
        hidden = sorted({r for w in desk.layers if not w.minimized and (r := privacy.sensitive_reason(w, s))})
        if hidden:
            lines.append(f"On the privacy list (blacked out in captures): {', '.join(hidden)}.")
        for problem in s.problems:
            lines.append(f"Config problem: {problem}")
        scope = privacy.share_scope(desk, s, desk.windows) if s.requires_approval else None
        return Result(window_scope=scope).text(*lines)

    @staticmethod
    def _safe_label(w: Window, s: Settings) -> str:
        return privacy.display_name(w, s)

    def _window_line(self, w: Window, desk: Desktop, s: Settings) -> str:
        tags = []
        if w.hwnd == desk.foreground:
            tags.append("focused")
        if w.host:
            tags.append("this chat")
        if w.hwnd == desk.under_cursor:
            tags.append("under mouse")
        if w.maximized:
            tags.append("maximized")
        if w.topmost:
            tags.append("always on top")
        r = w.rect
        return f"[{w.handle}] {self._safe_label(w, s)}, {r.w}x{r.h} at ({r.x}, {r.y})" + (f" ({', '.join(tags)})" if tags else "")

    # -- the user's own captures -------------------------------------------------------

    def latest_snip(self, count: int = 1, grid: bool | None = None) -> Result:
        s = self.guard()
        tr = self.tracker
        # Snips can't be redacted (Argus doesn't know what windows they show), so
        # the work profile skips the Screenshots folder unless the user opted in.
        files = not s.work or s.allow_snip_folder
        snips = latest_snips(count, tr.clip_time if tr else None, tr.clip_seq if tr else None, include_files=files)
        if not snips:
            if not files:
                raise ArgusError(
                    "No snip on the clipboard. The work profile doesn't read the Screenshots folder, so the user has to "
                    "copy the snip (Win+Shift+S puts it on the clipboard)."
                )
            raise ArgusError(
                f"No snips found: the clipboard holds no image and {screenshots_dir()} has no screenshots. "
                "The user can snip with Win+Shift+S."
            )
        shots = []
        for sn in snips:
            when = f"{ago(time.time() - sn.when)} ago" if sn.when else "before Argus started watching the clipboard"
            what = f"saved snip {sn.path.name}" if sn.path else "image on the clipboard"
            shots.append(
                Shot(image=sn.image, kind="snip", label=f"{what}, from {when}", origin=None, target={}, method=sn.source,
                     taken=sn.when or time.time(), source=sn.path, profile=s.profile, policy=s.policy)
            )
        result = Result()
        if len(shots) > 1:
            result.text(f"The user's {len(shots)} latest snips, newest first.")
        return self._show(result, shots, grid)

    def latest_mark(self, scope: str = "window", grid: bool | None = None) -> Result:
        s = self.guard()
        marks = recent_marks(1)
        if not marks:
            raise ArgusError(
                f"No hotkey marks yet. The user presses {pretty_hotkey(s.hotkey_mark)} over a window to mark it, "
                "and the Argus tray app has to be running (start it with `argus-tray`)."
            )
        mark = marks[0]
        shot = mark.shot(scope)
        if shot is None:
            raise ArgusError("The latest mark's image files are missing (cleaned up or deleted).")
        if not _profile_ok(shot, s):
            raise ArgusError(
                "The latest mark wasn't made under the user's current work profile settings (it predates switching to "
                "work or changing the allowed apps), so Argus won't share it. "
                f"They can mark again with {pretty_hotkey(s.hotkey_mark)}."
            )
        if not self.defer_marks:
            self.used_marks.add(mark.id)
        what = mark.info.get("monitor" if scope == "monitor" else "window") or mark.info.get("monitor")
        result = Result(mark_id=mark.id).text(f"Hotkey mark from {ago(time.time() - mark.when)} ago: {what}.")
        return self._show(result, [shot], grid)

    # -- OCR ---------------------------------------------------------------------------

    async def _ocr_targets(self, monitor, window, region, capture_id) -> list[Capture]:
        if capture_id:
            return [self.capture_ref(capture_id)]
        s = settings()
        shots = await anyio.to_thread.run_sync(lambda: capture(self.desk(), s, monitor, window, region))
        return [self.register(shot, grid=False) for shot in shots]

    async def read_text(self, monitor=None, window=None, region=None, capture_id=None, positions: bool = True) -> Result:
        self.guard()
        caps = await self._ocr_targets(monitor, window, region, capture_id)
        result = Result(sources=caps)
        for cap in caps:
            lines = await ocr.read(cap.image)
            head = f"[{cap.id}] Text in {cap.shot.label}"
            if positions:
                head += " ([x,y] = where each line starts, in this capture's image coordinates, as zoom uses them)"
            result.text(head + ":", ocr.as_text(lines, positions, scale=cap.view.scale))
            if cap.shot.hidden:
                result.text("Blacked-out (privacy) areas were not read: " + ", ".join(sorted(set(cap.shot.hidden))) + ".")
        return result

    async def find_text(self, text: str, monitor=None, window=None, region=None, capture_id=None) -> Result:
        self.guard()
        found: list[tuple[Capture, ocr.Match]] = []
        first_lines: list[ocr.Line] = []
        caps = await self._ocr_targets(monitor, window, region, capture_id)
        result = Result(sources=caps)
        for cap in caps:
            lines = await ocr.read(cap.image)
            first_lines = first_lines or lines
            found.extend((cap, m) for m in ocr.find(lines, text))
        if not found:
            sample = "; ".join(ln.text for ln in first_lines[:12]) or "nothing legible"
            return result.text(f'"{text}" is not on screen (OCR). Text it did read includes: {sample}.')
        found.sort(key=lambda cm: -cm[1].score)
        lines = [f'Found "{text}" {len(found)} time(s):']
        for i, (cap, m) in enumerate(found[:10], 1):
            sc = cap.view.scale
            x0, y0, x1, y1 = (round(v * sc) for v in m.box)
            where = f"in [{cap.id}] at box [{x0}, {y0}, {x1}, {y1}]"
            if cap.shot.origin is not None:
                cx = cap.shot.origin[0] + (m.box[0] + m.box[2]) / 2
                cy = cap.shot.origin[1] + (m.box[1] + m.box[3]) / 2
                where += f", desktop point ({round(cx)}, {round(cy)})"
            if cap.view.grid is not None:
                where += f", grid cell {cap.view.grid.cell_at((x0 + x1) / 2, (y0 + y1) / 2)}"
            exact = "" if m.score >= 0.95 else f" (fuzzy {m.score:.0%})"
            context = f' - line: "{m.line}"' if m.line.strip() != m.text.strip() else ""
            lines.append(f'{i}. "{m.text}"{exact} {where}{context}')
        cap, best = found[0]
        pad = 160
        img = cap.image
        bx0, by0 = max(0, int(best.box[0]) - pad), max(0, int(best.box[1]) - pad)
        bx1, by1 = min(img.width, int(best.box[2]) + pad), min(img.height, int(best.box[3]) + pad)
        origin = (cap.shot.origin[0] + bx0, cap.shot.origin[1] + by0) if cap.shot.origin else None
        crop = Shot(
            image=img.crop((bx0, by0, bx1, by1)), kind="zoom", label=f'best match for "{text}" (from {cap.id})',
            origin=origin, target={}, method="crop", cursor=cap.shot.cursor, taken=cap.shot.taken,
            share_windows=cap.shot.share_windows,
            profile=cap.shot.profile, policy=cap.shot.policy,
        )
        box = (best.box[0] - bx0, best.box[1] - by0, best.box[2] - bx0, best.box[3] - by0)
        return result.text(*lines).add(self.register(crop, grid=False, boxes=[box], upscale_to=900))

    # -- testing -------------------------------------------------------------------------

    def save_baseline(self, name: str, monitor=None, window=None, region=None, *, local_receipt: bool = False) -> Result:
        s = self.guard()
        shots = capture(self.desk(), s, monitor, window, region)
        if len(shots) != 1:
            raise ArgusError("A baseline is one monitor, window or region, not 'all'.")
        shot = shots[0]
        path = testing.save_baseline(name, shot)
        if local_receipt:
            return Result(screen_derived=False).text(f"Saved baseline {path.stem!r}.")
        cap = self.register(shot, grid=False, save=False)
        w, h = shot.image.size
        return Result().text(
            f"Saved baseline {name!r}: {shot.label}, {w}x{h} [{cap.id}].",
            f"File: {path}",
            f'Later, compare(baseline="{name}") re-captures the same target and boxes what changed.',
        )

    def compare(self, baseline: str | None = None, capture_a: str | None = None, capture_b: str | None = None,
                ignore=None, threshold: int = 24, show: str = "after") -> Result:
        s = self.guard()
        if baseline:
            before = testing.load_baseline(baseline, s)
            _require_profile(before, s)
            after = recapture(self.desk(), s, before.target)
            name_before = f"baseline {baseline!r} (saved {ago(time.time() - before.taken)} ago)"
        elif capture_a:
            before = self.capture_ref(capture_a).shot
            if capture_b:
                after = self.capture_ref(capture_b).shot
            else:
                if not before.target:
                    raise ArgusError(f"{capture_a} can't be re-taken (snip, mark or file); pass capture_b too.")
                after = recapture(self.desk(), s, before.target)
            name_before = f"{capture_a} ({ago(time.time() - before.taken)} ago)"
        else:
            raise ArgusError('Pass baseline="name", or capture_a (and optionally capture_b).')

        rects = []
        for r in ignore or []:
            try:
                rects.append(Rect(*(int(round(float(v))) for v in r)))
            except (TypeError, ValueError):
                raise ArgusError("ignore takes a list of [x, y, width, height] boxes") from None
        d = testing.diff(before.image, after.image, threshold=max(0, min(int(threshold), 255)), ignore=rects)
        _same_policy(s)
        result = Result(evidence=[before, after])
        if d.changed == 0:
            cap = self.register(after, grid=False)
            return result.text(f"No visible change between {name_before} and now [{cap.id}]." + (f" {d.note}" if d.note else ""))

        # Regions are in `before` pixels; map them onto `after` if its size differs.
        sx, sy = after.image.width / before.image.width, after.image.height / before.image.height
        boxes = [(r.x * sx, r.y * sy, r.right * sx, r.bottom * sy) for r in d.regions]
        if show == "both":
            half = (s.max_side - 12) // 2  # the pair fits in one max_side-wide image
            _lv, left = render(before.image, max_side=half)
            right_view, right = render(after.image, max_side=half, boxes=boxes)
            combo = side_by_side(left, right, labels=(name_before, "now (changes boxed)"))
            scope = None
            if before.share_windows is not None and after.share_windows is not None:
                scope = before.share_windows | after.share_windows
            shot = Shot(image=combo, kind="compare", label=f"{name_before} vs now", origin=None, target={}, method="composite",
                        profile=after.profile, share_windows=scope,
                        policy=after.policy if before.policy == after.policy else "")
            cap = self.register(shot, grid=False)
            sc, cs = right_view.scale, cap.view.scale
            ox, oy = left.width + 12, 26
            listed = [((x0 * sc + ox) * cs, (y0 * sc + oy) * cs, (x1 * sc + ox) * cs, (y1 * sc + oy) * cs) for x0, y0, x1, y1 in boxes]
        else:
            cap = self.register(after, grid=False, boxes=boxes)
            sc = cap.view.scale
            listed = [(x0 * sc, y0 * sc, x1 * sc, y1 * sc) for x0, y0, x1, y1 in boxes]
        items = "; ".join(f"#{i} [{round(x0)}, {round(y0)}, {round(x1)}, {round(y1)}]" for i, (x0, y0, x1, y1) in enumerate(listed, 1))
        result.text(
            f"{d.percent} of pixels changed between {name_before} and now, in {len(d.regions)} area(s), boxed and "
            f"numbered in [{cap.id}]: {items} (x1, y1, x2, y2 in that image).",
            d.note or "",
        )
        return result.add(cap)

    async def wait_for(self, until: str, text: str | None = None, monitor=None, window=None, region=None,
                       timeout: float = 30.0, progress: Callable[[float, float, str], Awaitable[None]] | None = None,
                       *, approve_frame: Callable[[Shot], Awaitable[None]] | None = None) -> Result:
        s = self.guard()
        timeout = max(1.0, min(float(timeout), 600.0))
        shots = await anyio.to_thread.run_sync(lambda: capture(self.desk(), s, monitor, window, region))
        if len(shots) != 1:
            raise ArgusError("wait_for watches one monitor, window or region, not 'all'.")
        pending = [shots[0]]
        target = shots[0].target
        approval_time = 0.0

        async def grab() -> Shot:
            nonlocal approval_time
            privacy.ensure_not_paused()
            # Every frame re-checks the profile: if it (or its lists) changed,
            # the frames taken so far are dropped and the wait ends.
            _same_policy(s)
            if pending:
                shot = pending.pop()
            else:
                try:
                    shot = await anyio.to_thread.run_sync(lambda: recapture(self.desk(), settings(), target))
                except ArgusError:
                    if approve_frame is not None:
                        raise ArgusError("The target can no longer be captured.") from None
                    raise
            if approve_frame is not None:
                requested_at = time.monotonic()
                await approve_frame(shot)
                approval_time += time.monotonic() - requested_at
            return shot

        async def report(elapsed: float, message: str) -> None:
            if progress is not None:
                await progress(elapsed, timeout, message)

        outcome = await testing.wait_for(
            grab, until, text=text, read=ocr.read, find=ocr.find, timeout=timeout, progress=report,
            clock=lambda: time.monotonic() - approval_time,
        )
        _same_policy(s)  # the last frame may predate a switch made during OCR / settling
        boxes = None
        if outcome.matches:
            boxes = [m.box for m in outcome.matches[:5]]
        elif outcome.diff is not None and outcome.diff.regions:
            boxes = [r.ltrb_tuple() for r in outcome.diff.regions[:8]]
        result = Result().text(("Done: " if outcome.ok else "Timed out: ") + f"{outcome.detail} after {outcome.elapsed:.1f}s.")
        if outcome.shot is not None:
            result.add(self.register(outcome.shot, grid=False, boxes=boxes))
        return result


def status_text() -> str:
    from . import __version__

    s = settings()
    state = privacy.pause_state()
    return "\n".join(
        [
            f"Argus {__version__}: {state.describe()}",
            f"Profile: {s.profile}" + (" (only allowed apps are visible)" if s.work else " (everything but the privacy list is visible)"),
            f"Data: {home()}",
            f"Settings: {config_path()}",
            f"Hotkeys: mark {pretty_hotkey(s.hotkey_mark)}, pause {pretty_hotkey(s.hotkey_pause)} (tray app)",
            f"Captures are deleted after {s.retention_days:g} days.",
            *(f"Config problem: {p}" for p in s.problems),
        ]
    )
