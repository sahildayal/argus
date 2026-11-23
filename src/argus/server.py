"""The Argus MCP server (stdio). Gives Claude Code, Claude Desktop, VS Code, Codex,
Gemini/Antigravity and any other MCP client eyes on every monitor."""

# No `from __future__ import annotations` here: the MCP SDK reads these
# signatures to build each tool's JSON schema.

import functools
import inspect
import logging
import logging.handlers
import sys
from contextvars import ContextVar
from typing import Annotated, Literal
from uuid import uuid4

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__, win32  # noqa: F401  (win32 first: DPI awareness)
from . import gate
from .capture import Shot
from .config import home, pretty_hotkey, settings
from .desktop import ArgusError, Paused
from .imaging import render
from .service import Result, Session, caption
from .text import display_arg

log = logging.getLogger("argus")
_session: Session | None = None
_policy: ContextVar[gate.Policy] = ContextVar("argus_approval_policy")


def session() -> Session:
    global _session
    if _session is None:
        _session = Session(defer_marks=True)
    return _session


def _content(result: Result) -> list[str | Image]:
    out: list[str | Image] = []
    for part in result.parts:
        if isinstance(part, str):
            if part.strip():
                out.append(part)
        else:
            out.append(caption(part))
            out.append(Image(data=part.view.data, format=part.view.mime))
    return out or ["(nothing)"]


def _disclose(
    tool: str, result: Result, *, separator: str | None = None, allow_trust: bool = True,
) -> str | list[str | Image]:
    """MCP disclosure table (CLI emits Result directly).

    Always check: look, screenshot, list_screens, latest_snip, latest_mark,
    read_text, find_text, zoom, compare, and wait_for. Only zoom/OCR from a
    shared in-memory source inherits approval. A wait reviews each consecutive
    window set, with the exact output blocks shown for the final set.
    Exempt: save_baseline's local receipt, and list_screens' paused status.
    """
    start = _policy.get()
    caps = result.captures
    if separator is None:
        out = _content(result)
        images = tuple(cap.view.data for cap in caps)
        text = tuple(part for part in out if isinstance(part, str))
    else:
        out = separator.join(p for p in result.parts if isinstance(p, str))
        images, text = (), (out,)

    shots = [*(cap.shot for cap in result.sources + caps), *result.evidence]
    windows = result.window_scope
    if shots and all(shot.share_windows is not None for shot in shots):
        windows = frozenset().union(*(shot.share_windows for shot in shots))
    derived = tool in ("zoom", "read_text", "find_text") and bool(result.sources) and all(
        cap.shared_at == start.stamp for cap in result.sources
    )
    can_trust = allow_trust and result.allow_trust and tool != "list_screens" and all(shot.allow_trust for shot in shots)
    gate.require(gate.request(
        tool, images, text, windows=windows, start=start, required=result.screen_derived,
        previously_approved=derived, query=result.query, allow_trust=can_trust,
    ))
    if result.query:
        request_text = f"{gate.QUERY_LABEL}: {result.query}"
        out = [request_text, *out] if isinstance(out, list) else f"{request_text}\n\n{out}"
    if start.enabled and separator is None:
        for cap in caps:
            cap.shared_at = start.stamp
    if result.mark_id:
        session().used_marks.add(result.mark_id)
    return out


def _errors(fn):
    """Turn Argus errors into messages the AI can act on, and anything unexpected
    into a short report. Full tracebacks are logged only with approval off."""

    def explain(exc: Exception, start: gate.Policy | None):
        if isinstance(exc, ToolError):
            return exc
        if isinstance(exc, ArgusError):
            if start is not None and not isinstance(exc, Paused):
                gate.require(gate.request(fn.__name__, text=(str(exc),), start=start, allow_trust=False))
            return ToolError(str(exc))
        personal = False
        if start is not None and not start.enabled:
            try:
                personal = not gate.policy().enabled
            except (OSError, ValueError, gate.GateStopped):
                pass
        if not personal:
            error_id = uuid4().hex
            log.error("%s (error id %s)", type(exc).__name__, error_id)
            return ToolError(f"Argus hit an unexpected error. Check the Argus server log for error id {error_id}.")
        log.error("tool %s failed", fn.__name__, exc_info=exc)
        return ToolError(f"Argus hit an unexpected error ({type(exc).__name__}: {exc}). Details: {home() / 'logs' / 'server.log'}")

    def stopped(exc: gate.GateStopped) -> str:
        if str(exc) == gate.UNAVAILABLE:
            raise ToolError(gate.UNAVAILABLE) from None
        return gate.DECLINED

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            start = token = None
            try:
                start = gate.policy()
                token = _policy.set(start)
                return await fn(*args, **kwargs)
            except gate.GateStopped as exc:
                return stopped(exc)
            except Exception as exc:
                try:
                    error = await anyio.to_thread.run_sync(explain, exc, start)
                except gate.GateStopped as stop:
                    return stopped(stop)
                raise error from None
            finally:
                if token is not None:
                    _policy.reset(token)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = token = None
        try:
            start = gate.policy()
            token = _policy.set(start)
            return fn(*args, **kwargs)
        except gate.GateStopped as exc:
            return stopped(exc)
        except Exception as exc:
            try:
                error = explain(exc, start)
            except gate.GateStopped as stop:
                return stopped(stop)
            raise error from None
        finally:
            if token is not None:
                _policy.reset(token)

    return wrapper


def _instructions() -> str:
    s = settings()
    return f"""Argus shows you the user's Windows desktop - every monitor and window.
- When the user points at something on screen without naming it ("look at this", "what I'm looking at", "this error", "my screen"), call look first. It skips the terminal/IDE you are running in and picks the window they were just using.
- For a named place use screenshot(monitor=...) or screenshot(window=...). Monitors are numbered left to right and also answer to "left", "right", "center", "laptop", "cursor". list_screens shows what's where, cheaply.
- Images can be downscaled to fit. Don't guess at small or blurry text: zoom(capture_id, box=[x1,y1,x2,y2]) in the coordinates of the image you saw, or read_text for exact text.
- The user can hand you captures: latest_snip (their Win+Shift+S snips / clipboard image) and latest_mark ({pretty_hotkey(s.hotkey_mark)} hotkey via the tray app).
- Testing: save_baseline + compare for visual regressions; wait_for to wait for a change, a settled screen, or some text.
- The Argus tray asks the user to approve sharing in the work profile and in personal when enabled. Respect a decline; if approval is unavailable, ask them to check the tray.
- Privacy is the user's call: some windows are blacked out by their privacy list and they can pause Argus entirely. In their work profile only the apps they allowed are visible; everything else is blacked out and listed without titles. Never try to work around any of this - tell the user instead."""


mcp = MCPServer("argus", title="Argus", version=__version__, instructions=_instructions())

RO = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

MonitorArg = Annotated[
    str | None,
    Field(description='Which monitor: a number (1 = leftmost), "left"/"center"/"right"/"top"/"bottom", "primary", "laptop", '
                      '"cursor" (where the mouse is), "active" (has the focused window), part of its model name, or "all".'),
]
WindowArg = Annotated[
    str | None,
    Field(description='Which window: part of its title or app name ("chrome", "localhost:3000", "Visual Studio Code"), '
                      '"active", "under_cursor", or a handle like "0x1A2B3C" from list_screens.'),
]
RegionArg = Annotated[
    list[float] | None,
    Field(description="[x, y, width, height] in pixels: desktop coordinates on its own, or relative to the monitor/window "
                      "when one is given too.", min_length=4, max_length=4),
]
GridArg = Annotated[bool | None, Field(description="Overlay a labeled grid (A1, B2, ...) so places can be named and zoomed by cell.")]
CaptureArg = Annotated[str | None, Field(description='A capture id such as "c3" from an earlier result (default: the latest one), or the path of a saved PNG.')]


@mcp.tool(title="Look at what the user is looking at", annotations=RO, structured_output=False)
@_errors
def look(
    scope: Annotated[Literal["window", "monitor"], Field(description='"window" (default), or "monitor" for the whole monitor that window is on.')] = "window",
    grid: GridArg = None,
    fresh: Annotated[bool, Field(description="Ignore a recent hotkey mark and capture live.")] = False,
) -> list[str | Image]:
    """See what the user is looking at right now. Call this first whenever they say "look at this", "what I'm looking at", "my screen", "this page/error/window", or refer to something on screen without naming it.

    It picks, in order: a hotkey mark they made in the last couple of minutes; else the window they were using just before switching to this chat (the terminal/IDE you run in is skipped); else the front-most other window. The reply says why and lists alternatives such as the window under the mouse - if it's the wrong one, use screenshot(window=...) or screenshot(monitor=...)."""
    return _disclose("look", session().look(scope, grid, fresh))


@mcp.tool(title="Screenshot a monitor, window or region", annotations=RO, structured_output=False)
@_errors
def screenshot(monitor: MonitorArg = None, window: WindowArg = None, region: RegionArg = None, grid: GridArg = None) -> list[str | Image]:
    """Capture a monitor, a window, or a region of the screen. Give monitor or window (optionally with a region inside it), or just a region; with nothing, every monitor comes back, one image each. Covered windows are rendered on their own. Large images arrive downscaled - use zoom to read fine detail."""
    return _disclose("screenshot", session().screenshot(monitor, window, region, grid))


@mcp.tool(title="Zoom into a capture", annotations=RO, structured_output=False)
@_errors
def zoom(
    box: Annotated[list[float] | None, Field(description="[x1, y1, x2, y2] in the pixels of the image you were shown.", min_length=4, max_length=4)] = None,
    cell: Annotated[str | None, Field(description='Grid cell such as "D7", or a range such as "C3:E5" (grid captures only).')] = None,
    capture_id: CaptureArg = None,
    grid: GridArg = None,
) -> list[str | Image]:
    """Zoom into part of an earlier capture at full resolution - use it to read small text or check detail instead of guessing. Small areas are enlarged up to 3x. Zooms are captures too, so you can zoom into a zoom."""
    return _disclose("zoom", session().zoom(box, cell, capture_id, grid))


@mcp.tool(title="List monitors and windows", annotations=RO, structured_output=False)
@_errors
def list_screens() -> str:
    """List the monitors (number, position, resolution, scaling, which one has the mouse) and the open windows on each, front to back, with handles and recent focus history. Text only, so it's cheap - use it to choose monitor= or window= for screenshot."""
    return _disclose("list_screens", session().list_screens(), separator="\n\n")


@mcp.tool(title="The user's latest snip", annotations=RO, structured_output=False)
@_errors
def latest_snip(
    count: Annotated[int, Field(description="How many recent snips to return, newest first.", ge=1, le=10)] = 1,
    grid: GridArg = None,
) -> list[str | Image]:
    """Get what the user snipped most recently: Win+Shift+S, Snipping Tool or PrtScn (saved to Pictures\\Screenshots), or an image they copied to the clipboard. Use it when they say "look at my snip", "I just took a screenshot", or "check what I copied"."""
    return _disclose("latest_snip", session().latest_snip(count, grid))


@mcp.tool(title="The user's latest hotkey mark", annotations=RO, structured_output=False)
@_errors
def latest_mark(
    scope: Annotated[Literal["window", "monitor"], Field(description='"window" (default) or "monitor" for the whole monitor it was on.')] = "window",
    grid: GridArg = None,
) -> list[str | Image]:
    """Get the window the user marked most recently with the Argus hotkey (pressed over a window; needs the Argus tray app). look already uses marks from the last couple of minutes; use this for older ones or when they say "what I marked"."""
    return _disclose("latest_mark", session().latest_mark(scope, grid))


@mcp.tool(title="Read text on screen (OCR)", annotations=RO, structured_output=False)
@_errors
async def read_text(
    monitor: MonitorArg = None,
    window: WindowArg = None,
    region: RegionArg = None,
    capture_id: CaptureArg = None,
    positions: Annotated[bool, Field(description="Prefix each line with its [x,y] position.")] = True,
) -> str:
    """Read the exact text on screen with Windows' built-in offline OCR: error messages, logs, code, dialogs. It's cheaper than an image for text-heavy screens and exact enough to quote. Target it like screenshot, or pass capture_id to read an earlier capture. Lines come back top to bottom."""
    result = await session().read_text(monitor, window, region, capture_id, positions)
    return await anyio.to_thread.run_sync(lambda: _disclose("read_text", result, separator="\n\n"))


@mcp.tool(title="Find text on screen", annotations=RO, structured_output=False)
@_errors
async def find_text(
    text: Annotated[str, Field(description="The text to find, e.g. a button label or an error message.")],
    monitor: MonitorArg = None,
    window: WindowArg = None,
    region: RegionArg = None,
    capture_id: CaptureArg = None,
) -> list[str | Image]:
    """Find where some text appears on screen with OCR: a button, a menu item, an error. Returns every match with its position in the capture and on the desktop, plus a zoomed image of the best match. Ignores case and tolerates small OCR mistakes."""
    result = await session().find_text(text, monitor, window, region, capture_id)
    return await anyio.to_thread.run_sync(_disclose, "find_text", result)


@mcp.tool(title="Save a visual baseline", annotations=WRITES, structured_output=False)
@_errors
def save_baseline(
    name: Annotated[str, Field(description="A short name, e.g. 'login-page' or 'settings-dark'.")],
    monitor: MonitorArg = None,
    window: WindowArg = None,
    region: RegionArg = None,
) -> str:
    """Save a reference screenshot of a monitor, window or region for visual testing; compare(baseline=name) later shows exactly what changed. Baselines are kept until deleted - they're exempt from the automatic cleanup."""
    result = session().save_baseline(name, monitor, window, region, local_receipt=_policy.get().enabled)
    return _disclose("save_baseline", result, separator="\n")


@mcp.tool(title="Compare screenshots", annotations=RO, structured_output=False)
@_errors
def compare(
    baseline: Annotated[str | None, Field(description="Name of a saved baseline to compare the screen against now.")] = None,
    capture_a: Annotated[str | None, Field(description="Earlier capture id (or PNG path). Alone, it's compared with a fresh capture of the same target.")] = None,
    capture_b: Annotated[str | None, Field(description="Second capture id to compare capture_a with.")] = None,
    ignore: Annotated[list[list[float]] | None, Field(description="Areas to ignore (clocks, animations): [x, y, width, height] boxes in the real pixels of the captured area.")] = None,
    threshold: Annotated[int, Field(description="Per-channel difference (0-255) treated as noise.", ge=0, le=255)] = 24,
    show: Annotated[Literal["after", "both"], Field(description='"after" (default): current image with changes boxed; "both": before and after side by side.')] = "after",
) -> list[str | Image]:
    """Visual diff. compare(baseline="name") re-captures that baseline's target and boxes every changed area; compare(capture_a="c2", capture_b="c5") diffs two captures; compare(capture_a="c2") diffs it against a fresh capture of the same thing. Says how much changed and where, with an image of numbered red boxes."""
    return _disclose("compare", session().compare(baseline, capture_a, capture_b, ignore, threshold, show))


@mcp.tool(title="Wait for the screen", annotations=RO, structured_output=False)
@_errors
async def wait_for(
    until: Annotated[Literal["change", "stable", "text", "text_gone"], Field(description='"change": anything changes; "stable": stops changing; "text": the text appears; "text_gone": it disappears.')],
    ctx: Context,
    text: Annotated[str | None, Field(description='Text for until="text" or "text_gone", e.g. "Compiled successfully" or "Loading".')] = None,
    monitor: MonitorArg = None,
    window: WindowArg = None,
    region: RegionArg = None,
    timeout: Annotated[float, Field(description="Seconds to wait before giving up (max 600).", ge=1, le=600)] = 30,
) -> list[str | Image]:
    """Wait for something on screen, then return a screenshot of it: a build finishing, a page loading, a dialog appearing, "Loading..." going away. Target it like screenshot (one monitor, window or region). Polls about twice a second; text checks use OCR about once a second."""

    async def progress(elapsed: float, total: float, message: str) -> None:
        try:
            await ctx.report_progress(elapsed, total, message)
        except Exception:  # progress is best-effort; some clients don't ask for it
            pass

    start = _policy.get()
    previous: Shot | None = None
    changed = False
    query = display_arg(text) if text and until in ("text", "text_gone") else ""

    def review_segment(shot: Shot) -> None:
        nonlocal previous, changed
        if gate.policy() != start:
            raise gate.GateStopped(gate.DECLINED)
        if previous is not None and (shot.share_windows is None or previous.share_windows != shot.share_windows):
            # Hold all MCP output until the end. On a transition review the
            # previous set before the new frame can contribute to the result.
            view, _ = render(previous.image, max_side=settings().max_side)
            gate.require(gate.request(
                "wait_for", (view.data,), windows=previous.share_windows, start=start,
                watching=True, allow_trust=not changed and previous.allow_trust, query=query,
            ))
            changed = True
        previous = shot

    async def review_frame(shot: Shot) -> None:
        await anyio.to_thread.run_sync(review_segment, shot)

    result = await session().wait_for(
        until, text, monitor, window, region, timeout, None if start.enabled else progress,
        approve_frame=review_frame if start.enabled else None,
    )
    return await anyio.to_thread.run_sync(lambda: _disclose("wait_for", result, allow_trust=not changed))


def _setup_logging() -> None:
    logs = home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "server.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    stderr = logging.StreamHandler(sys.stderr)  # never stdout: that's the MCP channel
    stderr.setLevel(logging.WARNING)
    log.addHandler(stderr)


def main() -> None:
    _setup_logging()
    s = session()  # detect the host window now, while it's most likely in front
    host = s.host
    log.info("argus %s starting; host chain %s; exact host windows %s; dpi %s", __version__, host.names, sorted(host.exact), win32.DPI_AWARENESS)
    from .privacy import cleanup

    cleanup(settings(), force=True)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
