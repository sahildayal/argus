"""`argus` on the command line: the same powers as the MCP server, for scripts,
tests, and AI tools that can run shell commands but don't speak MCP.

Captures print their details and the path of the saved PNG (the full-resolution
image); `--json` gives machine-readable output; `argus wait` exits 0 when the
condition was met and 1 on timeout, so it slots into test scripts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__, win32  # noqa: F401  (win32 first: DPI awareness)
from .desktop import ArgusError


def _region(text: str) -> list[float]:
    parts = [p for p in text.replace(" ", "").split(",") if p]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("use x,y,width,height")
    return [float(p) for p in parts]


def _box(text: str) -> list[float]:
    parts = [p for p in text.replace(" ", "").split(",") if p]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("use x1,y1,x2,y2")
    return [float(p) for p in parts]


def _targets(p: argparse.ArgumentParser) -> None:
    p.add_argument("-m", "--monitor", help='number (1 = leftmost), left/center/right/top/bottom, primary, laptop, cursor, active, all')
    p.add_argument("-w", "--window", help='part of a title or app name, "active", "under_cursor", or a handle like 0x1A2B3C')
    p.add_argument("-r", "--region", type=_region, help="x,y,width,height (relative to --monitor/--window if given)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="argus", description="Multi-monitor screenshots for AI tools.")
    ap.add_argument("--version", action="version", version=f"argus {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("look", help="capture the window you were just using (skips this terminal)")
    p.add_argument("--monitor-scope", action="store_true", help="capture that window's whole monitor")
    p.add_argument("--fresh", action="store_true", help="ignore a recent hotkey mark")
    p.add_argument("--grid", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--out", type=Path, help="also copy the PNG here")

    p = sub.add_parser("shot", help="capture a monitor, window or region (default: every monitor)")
    _targets(p)
    p.add_argument("--grid", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--out", type=Path, help="also copy the PNG here (a folder when several monitors)")

    p = sub.add_parser("list", help="monitors and windows")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("zoom", help="crop a saved capture at full resolution")
    p.add_argument("file", help="a PNG from an earlier capture")
    p.add_argument("--box", type=_box, help="x1,y1,x2,y2 in the image's pixels")
    p.add_argument("--json", action="store_true")
    p.add_argument("-o", "--out", type=Path)

    p = sub.add_parser("snip", help="your latest Win+Shift+S snip / clipboard image")
    p.add_argument("-n", "--count", type=int, default=1)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("mark", help="what the hotkey does: mark the window under the mouse now")

    p = sub.add_parser("ocr", help="read text on screen")
    _targets(p)
    p.add_argument("-f", "--file", help="read a saved image instead")
    p.add_argument("--plain", action="store_true", help="no [x,y] positions")

    p = sub.add_parser("find", help="find text on screen")
    p.add_argument("text")
    _targets(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("baseline", help="visual-test baselines")
    bsub = p.add_subparsers(dest="action", required=True)
    b = bsub.add_parser("save")
    b.add_argument("name")
    _targets(b)
    b = bsub.add_parser("compare")
    b.add_argument("name")
    b.add_argument("--both", action="store_true", help="before/after side by side")
    b.add_argument("--json", action="store_true")
    bsub.add_parser("list")
    b = bsub.add_parser("delete")
    b.add_argument("name")

    p = sub.add_parser("wait", help="wait for the screen; exit 0 when met, 1 on timeout")
    p.add_argument("until", choices=["change", "stable", "text", "text_gone"])
    p.add_argument("--text")
    _targets(p)
    p.add_argument("-t", "--timeout", type=float, default=30)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pause", help="block all captures (from every AI tool)")
    p.add_argument("--minutes", type=float, help="resume automatically after this long")
    sub.add_parser("resume", help="allow captures again")
    sub.add_parser("status", help="paused or not, paths, hotkeys")
    p = sub.add_parser("profile", help="show or switch the privacy profile (work = only allowed apps are visible)")
    p.add_argument("name", nargs="?", choices=["personal", "work"])
    p = sub.add_parser("allow", help="manage apps and title patterns visible in the work profile")
    p.add_argument("action", nargs="?", choices=["add", "remove", "add-title", "remove-title"])
    p.add_argument("values", nargs="*")
    p.add_argument("--learn", action="store_true", help="choose from open and recently focused apps")
    p.add_argument("--browser", action="store_true", help="apply a title pattern to browser tabs")
    p = sub.add_parser("cleanup", help="delete captures older than the retention period")
    p.add_argument("--all", action="store_true", help="delete every saved capture and mark now")

    p = sub.add_parser("audit", help="gate decision audit log")
    p.add_argument("--since", help="filter entries since ISO date (YYYY-MM-DD)")
    p.add_argument("-n", "--count", type=int, default=20, help="max entries to show (default 20)")
    asub = p.add_subparsers(dest="audit_action", required=False)
    asub.add_parser("verify", help="verify tamper-evidence hash chain")
    e = asub.add_parser("export", help="copy audit log to a file for compliance")
    e.add_argument("-o", "--out", type=Path, required=True, help="output .jsonl file")
    e.add_argument("--since", help="filter entries since ISO date (YYYY-MM-DD)")

    sub.add_parser("tray", help="run the tray app (hotkeys) in this console")
    sub.add_parser("mcp", help="run the MCP server on stdio")

    p = sub.add_parser("install", help="register Argus with your AI tools and start the tray with Windows")
    p.add_argument("--clients", help="comma list (default: every detected one): " + ", ".join(_client_keys()))
    p.add_argument("--trust", action="store_true",
                   help="pre-approve Argus in Claude Code and Gemini CLI so captures don't ask each time")
    p.add_argument("--no-startup", action="store_true", help="don't start the tray app with Windows")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("uninstall", help="remove Argus from your AI tools and Windows startup")
    p.add_argument("--purge", action="store_true", help="also delete ~/.argus (captures, marks, baselines, settings)")
    sub.add_parser("doctor", help="check that everything works")
    return ap


def _client_keys() -> list[str]:
    from .install import CLIENTS

    return [c.key for c in CLIENTS]


def _emit(result, args, out: Path | None = None) -> None:
    from .service import caption

    caps = result.captures
    parts = result.parts
    if result.query:
        parts = [f"Requested text: {result.query}", *parts]
    if getattr(args, "json", False):
        payload = {
            "text": [p for p in parts if isinstance(p, str)],
            "captures": [
                {"id": c.id, "label": c.shot.label, "path": str(c.path) if c.path else None,
                 "width": c.image.width, "height": c.image.height, "notes": c.shot.notes, "hidden": c.shot.hidden}
                for c in caps
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        for part in parts:
            print(part if isinstance(part, str) else caption(part))
            print()
    if out is not None and caps:
        if len(caps) == 1 and out.suffix:
            out.parent.mkdir(parents=True, exist_ok=True)
            caps[0].image.save(out)
            print(f"Copied to {out}")
        else:
            out.mkdir(parents=True, exist_ok=True)
            for c in caps:
                dest = out / f"{c.id}.png"
                c.image.save(dest)
                print(f"Copied to {dest}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles can contain any Unicode
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    try:
        return _run(args) or 0
    except ArgusError as exc:
        from .errors import scrub

        print(f"argus: {scrub(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from .errors import describe_unexpected

        message = describe_unexpected(exc)
        if message is None:
            raise
        print(f"argus: {message}", file=sys.stderr)
        return 1


def _run(args) -> int | None:
    cmd = args.cmd
    if cmd == "mcp":
        from .server import main as serve

        serve()
        return 0
    if cmd == "tray":
        from .tray import main as tray

        tray()
        return 0
    if cmd in ("install", "uninstall", "doctor"):
        from . import install

        return getattr(install, cmd)(args)
    if cmd in ("pause", "resume", "status", "cleanup"):
        return _privacy_cmd(args)
    if cmd == "audit":
        return _audit_cmd(args)
    if cmd == "profile":
        return _profile_cmd(args)
    if cmd == "allow":
        return _allow_cmd(args)

    from .service import Session

    session = Session(track=False)
    if cmd == "look":
        _emit(session.look("monitor" if args.monitor_scope else "window", args.grid or None, args.fresh), args, args.out)
    elif cmd == "shot":
        _emit(session.screenshot(args.monitor, args.window, args.region, args.grid or None), args, args.out)
    elif cmd == "list":
        result = session.list_screens()
        if args.json:
            print(json.dumps(_list_json(session), indent=2))
        else:
            print(result.parts[0])
    elif cmd == "zoom":
        if not args.box:
            raise ArgusError("--box x1,y1,x2,y2 is required")
        _emit(session.zoom(args.box, None, args.file), args, args.out)
    elif cmd == "snip":
        _emit(session.latest_snip(args.count), args)
    elif cmd == "mark":
        from . import privacy
        from .capture import capture_monitor, capture_window
        from .config import settings
        from .desktop import NO_HOST, snapshot
        from .marks import save_mark

        s = settings()
        privacy.ensure_not_paused()
        desk = snapshot(NO_HOST)
        win = desk.window_under_cursor or desk.foreground_window
        window_shot, problem = None, ""
        if win is not None:
            try:
                window_shot = capture_window(desk, win, s)
            except ArgusError as exc:
                problem = str(exc)
        mark = save_mark(window_shot, capture_monitor(desk, desk.cursor_monitor, s), extra={"problem": problem} if problem else None)
        what = privacy.display_name(win, s) if window_shot is not None and win else desk.cursor_monitor.title
        print(f"Marked {what} ({mark.id})" + (f" - window not captured: {problem}" if problem else ""))
    elif cmd == "ocr":
        if args.file:
            result = asyncio.run(session.read_text(capture_id=args.file, positions=not args.plain))
        else:
            result = asyncio.run(session.read_text(args.monitor, args.window, args.region, positions=not args.plain))
        print("\n\n".join(p for p in result.parts if isinstance(p, str)))
    elif cmd == "find":
        _emit(asyncio.run(session.find_text(args.text, args.monitor, args.window, args.region)), args)
    elif cmd == "baseline":
        return _baseline(session, args)
    elif cmd == "wait":
        result = asyncio.run(session.wait_for(args.until, args.text, args.monitor, args.window, args.region, args.timeout))
        _emit(result, args)
        first = next((p for p in result.parts if isinstance(p, str)), "")
        return 0 if first.startswith("Done") else 1
    return 0


def _baseline(session, args) -> int:
    from . import testing

    if args.action == "save":
        print(session.save_baseline(args.name, args.monitor, args.window, args.region).parts[0])
    elif args.action == "compare":
        result = session.compare(baseline=args.name, show="both" if args.both else "after")
        _emit(result, args)
        first = next((p for p in result.parts if isinstance(p, str)), "")
        return 0 if first.startswith("No visible change") else 1
    elif args.action == "list":
        import time

        from .config import settings

        s = settings()
        # Work profile: baselines from the personal profile or an older policy
        # are only counted - their names and labels may describe hidden things.
        rows, hidden = testing.baselines(s if s.work else None)
        if not rows and not hidden:
            print("No baselines yet.")
        for name, path, taken, label in rows:
            print(f"{name:24} {time.strftime('%Y-%m-%d %H:%M', time.localtime(taken))}  {label}  ({path})")
        if hidden:
            print(f"{hidden} hidden baseline(s): not saved under the current work profile.")
    elif args.action == "delete":
        print("Deleted." if testing.delete_baseline(args.name) else f"No baseline named {args.name!r}.")
    return 0


def _audit_cmd(args) -> int:
    from . import audit

    action = getattr(args, "audit_action", None)
    if action == "verify":
        valid, msg = audit.verify_chain()
        if valid:
            print(msg)
            return 0
        else:
            print(f"argus audit: {msg}", file=sys.stderr)
            return 1
    elif action == "export":
        since = getattr(args, "since", None)
        out = args.out
        try:
            count = audit.export_log(
                out, since=since, report_skipped=lambda n: print(f"Skipped {n} unparseable audit line(s)."),
            )
        except (OSError, ValueError) as exc:
            print(f"argus audit: {exc}", file=sys.stderr)
            return 1
        print(f"Exported {count} audit entry(ies) to {out}")
        return 0
    else:
        since = getattr(args, "since", None)
        count = getattr(args, "count", 20)
        entries = audit.read_entries(since=since, limit=None if since else count)
        print(audit.format_table(entries))
        return 0


def _privacy_cmd(args) -> int:
    from . import privacy
    from .config import settings
    from .service import status_text

    if args.cmd == "pause":
        state = privacy.pause(args.minutes)
        print(f"Argus {state.describe()}. AI tools can't capture anything until you resume (argus resume).")
    elif args.cmd == "resume":
        privacy.resume()
        print("Argus resumed.")
    elif args.cmd == "status":
        print(status_text())
    elif args.cmd == "cleanup":
        if args.all:
            privacy.purge_all()
            print("Deleted every saved capture and mark (baselines kept).")
        else:
            n = privacy.cleanup(settings(), force=True)
            print(f"Deleted {n} old file(s).")
    return 0


def _profile_cmd(args) -> int:
    from . import config

    if args.name:
        try:
            s = config.set_profile(args.name)
        except ValueError as exc:
            if config.settings().work and args.name.strip().lower() == "work":
                raise ArgusError(f"{exc}. The work profile is on anyway: Argus remembers the switch.") from None
            raise ArgusError(str(exc)) from None
        print(f"Switched to the {s.profile} profile. It applies to the next capture.")
    else:
        s = config.settings()
        print(f"Active profile: {s.profile}")
    if s.work:
        apps = ", ".join(sorted(s.allow_apps)) or "none"
        if s.profile_reason:
            print(f"Why work: {s.profile_reason}.")
        print("Only allowed windows are visible; everything else is blacked out and titles are hidden.")
        print(f"Allowed apps: {apps}")
        print(f"Allowed browser-tab patterns: {', '.join(p.pattern for p in s.allow_browser_titles) or 'none'}")
        if s.allow_titles:
            print(f"Allowed title patterns: {', '.join(p.pattern for p in s.allow_titles)}")
        if s.allow_browser_titles or s.allow_titles:
            print("Note: title patterns match titles that pages/programs set themselves - a convenience, not a security "
                  "boundary. For strict control, allow a dedicated browser by exe name instead.")
        if not (s.allow_apps or s.allow_browser_titles or s.allow_titles):
            print(f"Nothing is allowed yet, so captures will be fully black. Add apps under [work] in {config.config_path()}.")
    else:
        print("Everything is visible except the [privacy] block list. `argus profile work` switches to the allow list.")
    for problem in s.problems:
        print(f"Config problem: {problem}")
    return 0


def _allow_cmd(args) -> int:
    from . import config, privacy
    from .desktop import NO_HOST, _SHELL_CLASSES, _SHELL_PROCESSES, snapshot
    from .service import Session

    s = config.settings()
    if args.learn:
        if args.action or args.values or args.browser:
            raise ArgusError("use `argus allow --learn` by itself")
        desk = snapshot(NO_HOST)
        tracker = Session(track=True).tracker
        try:
            history = tracker.recent() if tracker else []
            by_hwnd = {w.hwnd: w for w in desk.layers}
            candidates = {}
            for w in [*desk.windows, *(by_hwnd[h] for _, h in history if h in by_hwnd)]:
                exe = w.process.strip()
                if (not w.is_app or not exe or not exe.lower().endswith(".exe") or not w.pid
                        or w.cls in _SHELL_CLASSES or exe.lower() in _SHELL_PROCESSES
                        or exe.lower() in {"argus.exe", "argus-tray.exe", "python.exe", "pythonw.exe"}):
                    continue
                candidates.setdefault(exe.lower(), (w.app or exe, exe))
        finally:
            if tracker: tracker.stop()
        rows = sorted(candidates.values(), key=lambda x: x[0].casefold())
        if not rows:
            print("No eligible apps found.")
            return 0
        for i, (app, exe) in enumerate(rows, 1):
            print(f"{i}. {app} — {exe}" + (" (allowed)" if exe.lower() in s.allow_apps else ""))
        print("Enter numbers to allow, e.g. 1 3 5, or 'a' for all, blank to cancel")
        def ask() -> str:
            try:
                return input("> ").strip().lower()
            except EOFError:
                return ""

        choice = ask()
        if not choice:
            print("Cancelled.")
            return 0
        try:
            picks = [int(n) for n in choice.replace(",", " ").split()] if choice != "a" else []
        except ValueError:
            raise ArgusError("enter valid numbers, or 'a'") from None
        if any(not 1 <= n <= len(rows) for n in picks):
            raise ArgusError(f"numbers must be between 1 and {len(rows)}")
        selected = rows if choice == "a" else [rows[n - 1] for n in dict.fromkeys(picks)]
        print("Allow " + ", ".join(exe for _, exe in selected) + "? [y/N]", flush=True)
        if ask() not in {"y", "yes"}:
            print("Cancelled.")
            return 0
        config.set_work_list("allow_apps", [*s.allow_apps, *(exe for _, exe in selected)])
        print("Updated allowed apps.")
        return 0
    if not args.action:
        print("Allowed apps: " + (", ".join(sorted(s.allow_apps)) or "none"))
        print("Allowed browser title patterns: " + (", ".join(p.pattern for p in s.allow_browser_titles) or "none"))
        print("Allowed title patterns: " + (", ".join(p.pattern for p in s.allow_titles) or "none"))
        return 0
    if args.action in ("add", "remove"):
        if not args.values or any(not v.lower().endswith(".exe") for v in args.values):
            raise ArgusError("provide one or more executable names ending in .exe")
        current = list(s.allow_apps)
        if args.action == "add": current.extend(args.values)
        else:
            removed = {v.lower() for v in args.values}
            current = [v for v in current if v.lower() not in removed]
        config.set_work_list("allow_apps", current)
    else:
        if len(args.values) != 1:
            raise ArgusError(f"{args.action} requires one regex")
        key = "allow_browser_titles" if args.browser else "allow_titles"
        patterns = list(getattr(s, key))
        values = [p.pattern for p in patterns]
        if args.action == "add-title": values.append(args.values[0])
        else: values = [v for v in values if v != args.values[0]]
        config.set_work_list(key, values)
    print("Updated work allow list.")
    return 0


def _list_json(session) -> dict:
    from . import privacy
    from .config import settings

    s = settings()
    desk = session.desk()

    def title(w) -> str:
        if not privacy.allowed(w, s):
            return "(hidden: work profile)"
        return "(hidden: privacy list)" if privacy.sensitive_reason(w, s) else w.title

    return {
        "monitors": [
            {"index": m.index, "position": m.position, "name": m.name, "device": m.device, "primary": m.primary,
             "builtin": m.builtin, "x": m.rect.x, "y": m.rect.y, "width": m.rect.w, "height": m.rect.h, "scale": m.scale}
            for m in desk.monitors
        ],
        "windows": [
            {"handle": w.handle, "title": title(w), "app": w.app, "process": w.process, "monitor": w.monitor,
             "x": w.rect.x, "y": w.rect.y, "width": w.rect.w, "height": w.rect.h, "minimized": w.minimized,
             "focused": w.hwnd == desk.foreground, "host": w.host}
            for w in desk.windows
        ],
        "cursor": list(desk.cursor),
    }


if __name__ == "__main__":
    sys.exit(main())
