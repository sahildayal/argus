"""Registering Argus with the AI tools on this machine and starting the tray app
with Windows. Everything here is undone by `argus uninstall`.

Each client is configured through its own CLI where it has one (claude, codex,
gemini, agy), so their config formats stay their business. VS Code and Claude
Desktop only have JSON files; those are backed up before they're edited."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import home
from .desktop import ArgusError

NAME = "argus"
APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
STARTUP_LINK = APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Argus.lnk"


def entry_point(name: str) -> str:
    """Absolute path of one of Argus's own executables (argus-mcp, argus-tray)."""
    found = shutil.which(name)
    if found:
        path = Path(found)
        return str(path.with_suffix(path.suffix.lower()))  # PATHEXT hands back ".EXE"
    scripts = Path(sys.executable).parent
    for candidate in (scripts / f"{name}.exe", scripts / "Scripts" / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    raise ArgusError(f"Can't find {name}.exe. Install Argus first: uv tool install --editable <path to argus>")


def _tool(name: str, *fallbacks: Path) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return next((str(p) for p in fallbacks if p.exists()), None)


def _run(cmd: list[str], dry: bool) -> tuple[bool, str]:
    if dry:
        return True, "would run: " + " ".join(cmd)
    if cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + f".bak-argus-{time.strftime('%Y%m%d-%H%M%S')}"))


def _load_jsonc(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        pass
    # VS Code allows comments and trailing commas; strip them outside strings.
    out, i, in_str = [], 0, False
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1 : i + 2])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith("/*", i):
            i = text.find("*/", i)
            i = len(text) if i < 0 else i + 2
            continue
        else:
            out.append(ch)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        return json.loads(cleaned)
    except ValueError as exc:
        raise ArgusError(f"{path} isn't valid JSON ({exc}); edit it by hand to add Argus.") from None


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ clients


@dataclass
class Client:
    key: str
    name: str
    detect: Callable[[], bool]
    add: Callable[[str, bool, bool], str]  # (server exe, allow without prompts, dry run) -> what happened
    remove: Callable[[bool], str]
    check: Callable[[], str]


def _claude_code() -> Client:
    exe = lambda: _tool("claude", Path.home() / ".local" / "bin" / "claude.exe")  # noqa: E731
    settings_path = Path.home() / ".claude" / "settings.json"

    def add(server: str, allow: bool, dry: bool) -> str:
        _run([exe(), "mcp", "remove", NAME, "-s", "user"], dry)  # replace any older registration
        ok, out = _run([exe(), "mcp", "add", "-s", "user", NAME, "--", server], dry)
        if not ok:
            raise ArgusError(f"claude mcp add failed: {out}")
        if dry:
            return out + (" (+ pre-approve mcp__argus in ~/.claude/settings.json)" if allow else "")
        note = "registered (user scope)"
        if allow and not dry:
            data = _load_jsonc(settings_path)
            rules = data.setdefault("permissions", {}).setdefault("allow", [])
            if "mcp__argus" not in rules:
                _backup(settings_path)
                rules.append("mcp__argus")
                _save_json(settings_path, data)
            note += "; captures pre-approved (no permission prompts)"
        return note

    def remove(dry: bool) -> str:
        _run([exe(), "mcp", "remove", NAME, "-s", "user"], dry)
        if not dry and settings_path.exists():
            data = _load_jsonc(settings_path)
            rules = data.get("permissions", {}).get("allow", [])
            if "mcp__argus" in rules:
                _backup(settings_path)
                rules.remove("mcp__argus")
                _save_json(settings_path, data)
        return "removed"

    def check() -> str:
        ok, out = _run([exe(), "mcp", "get", NAME], False)
        return "registered" if ok and NAME in out else "not registered"

    return Client("claude-code", "Claude Code", lambda: exe() is not None, add, remove, check)


def _claude_desktop() -> Client:
    path = APPDATA / "Claude" / "claude_desktop_config.json"

    def add(server: str, _allow: bool, dry: bool) -> str:
        if dry:
            return f"would add mcpServers.argus to {path}"
        data = _load_jsonc(path)
        _backup(path)
        data.setdefault("mcpServers", {})[NAME] = {"command": server, "args": []}
        _save_json(path, data)
        return "added to claude_desktop_config.json (quit and reopen Claude Desktop to load it)"

    def remove(dry: bool) -> str:
        if dry or not path.exists():
            return "nothing to remove" if not path.exists() else f"would edit {path}"
        data = _load_jsonc(path)
        if NAME in data.get("mcpServers", {}):
            _backup(path)
            del data["mcpServers"][NAME]
            _save_json(path, data)
        return "removed"

    def check() -> str:
        return "registered" if NAME in _load_jsonc(path).get("mcpServers", {}) else "not registered"

    return Client("claude-desktop", "Claude Desktop", lambda: path.parent.exists(), add, remove, check)


def _vscode() -> Client:
    path = APPDATA / "Code" / "User" / "mcp.json"

    def add(server: str, _allow: bool, dry: bool) -> str:
        if dry:
            return f"would add servers.argus to {path}"
        data = _load_jsonc(path)
        _backup(path)
        data.setdefault("servers", {})[NAME] = {"type": "stdio", "command": server, "args": []}
        _save_json(path, data)
        return "added to user mcp.json (Copilot agent mode / any MCP extension)"

    def remove(dry: bool) -> str:
        if dry or not path.exists():
            return "nothing to remove" if not path.exists() else f"would edit {path}"
        data = _load_jsonc(path)
        if NAME in data.get("servers", {}):
            _backup(path)
            del data["servers"][NAME]
            _save_json(path, data)
        return "removed"

    def check() -> str:
        return "registered" if NAME in _load_jsonc(path).get("servers", {}) else "not registered"

    return Client("vscode", "VS Code", lambda: path.parent.exists(), add, remove, check)


def _cli_client(key: str, name: str, exe: Callable[[], str | None], add_args, remove_args, list_args) -> Client:
    def add(server: str, allow: bool, dry: bool) -> str:
        _run([exe(), *remove_args], dry)
        ok, out = _run([exe(), *add_args(server, allow)], dry)
        if not ok:
            raise ArgusError(f"{name}: {out[-400:]}")
        return out if dry else "registered"

    def remove(dry: bool) -> str:
        ok, out = _run([exe(), *remove_args], dry)
        return "removed" if ok else f"not removed ({out[-200:]})"

    def check() -> str:
        ok, out = _run([exe(), *list_args], False)
        return "registered" if ok and re.search(rf"\b{NAME}\b", out) else "not registered"

    return Client(key, name, lambda: exe() is not None, add, remove, check)


def _codex() -> Client:
    return _cli_client(
        "codex", "Codex", lambda: _tool("codex"),
        lambda server, _allow: ["mcp", "add", NAME, "--", server],
        ["mcp", "remove", NAME], ["mcp", "list"],
    )


def _gemini() -> Client:
    return _cli_client(
        "gemini", "Gemini CLI", lambda: _tool("gemini"),
        lambda server, allow: ["mcp", "add", "-s", "user", *(["--trust"] if allow else []), NAME, server],
        ["mcp", "remove", "-s", "user", NAME], ["mcp", "list"],
    )


def _antigravity() -> Client:
    return _cli_client(
        "antigravity", "Antigravity (agy)", lambda: _tool("agy", LOCALAPPDATA / "agy" / "bin" / "agy.exe"),
        lambda server, _allow: ["mcp", "add", NAME, server],
        ["mcp", "remove", NAME], ["mcp", "list"],
    )


CLIENTS = [_claude_code(), _claude_desktop(), _vscode(), _codex(), _gemini(), _antigravity()]


# ------------------------------------------------------------------ startup


def add_startup(tray: str, dry: bool) -> str:
    if dry:
        return f"would create {STARTUP_LINK}"
    ps = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{STARTUP_LINK}');"
        f"$s.TargetPath='{tray}';$s.WorkingDirectory='{home()}';"
        "$s.Description='Argus: screenshot hotkeys for AI tools';$s.Save()"
    )
    ok, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], False)
    if not ok:
        raise ArgusError(f"couldn't create the startup shortcut: {out}")
    return "tray app starts with Windows"


def start_tray(tray: str) -> None:
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([tray], creationflags=flags, close_fds=True, cwd=str(home()))


def tray_running() -> bool:
    from . import win32

    handle = win32.CreateMutexW(None, False, "Local\\ArgusTray")
    import ctypes

    exists = ctypes.get_last_error() == win32.ERROR_ALREADY_EXISTS
    win32.CloseHandle(handle)
    return exists


# ----------------------------------------------------------------- commands


def _selected(args) -> list[Client]:
    if getattr(args, "clients", None):
        wanted = {k.strip().lower() for k in args.clients.split(",") if k.strip()}
        unknown = wanted - {c.key for c in CLIENTS}
        if unknown:
            raise ArgusError(f"unknown client(s): {', '.join(sorted(unknown))}. Known: {', '.join(c.key for c in CLIENTS)}")
        return [c for c in CLIENTS if c.key in wanted]
    return [c for c in CLIENTS if c.detect()]


def install(args) -> int:
    server = entry_point("argus-mcp")
    allow = args.trust  # default: each AI tool keeps asking before a capture
    print(f"Argus MCP server: {server}")
    failures = 0
    for client in _selected(args):
        try:
            print(f"  {client.name:18} {client.add(server, allow, args.dry_run)}")
        except ArgusError as exc:
            failures += 1
            print(f"  {client.name:18} FAILED: {exc}")
    if not args.no_startup:
        tray = entry_point("argus-tray")
        print(f"  {'Windows startup':18} {add_startup(tray, args.dry_run)}")
        if not args.dry_run and not tray_running():
            start_tray(tray)
            print(f"  {'Tray app':18} started")
    print("\nOpen a new session in each tool to pick Argus up (running ones don't reload MCP servers).")
    if not allow:
        print("Your AI tools will ask before each capture. To pre-approve Argus instead: argus install --trust")
    return 1 if failures else 0


def uninstall(args) -> int:
    for client in CLIENTS:
        if client.detect():
            print(f"  {client.name:18} {client.remove(False)}")
    if STARTUP_LINK.exists():
        STARTUP_LINK.unlink()
        print(f"  {'Windows startup':18} removed")
    if tray_running():
        _run(["taskkill", "/IM", "argus-tray.exe", "/F"], False)
        print(f"  {'Tray app':18} stopped")
    if args.purge:
        shutil.rmtree(home(), ignore_errors=True)
        print(f"  {'Data':18} deleted {home()}")
    return 0


def doctor(_args) -> int:
    from . import privacy, win32
    from .config import settings
    from .desktop import snapshot
    from .snips import screenshots_dir

    s = settings()
    print(f"DPI awareness: {win32.DPI_AWARENESS}")
    desk = snapshot()
    for m in desk.monitors:
        print(f"Monitor: {m.describe()}")
    try:
        from winrt.windows.media.ocr import OcrEngine

        langs = [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
        print(f"OCR: available ({', '.join(langs)})")
    except Exception as exc:  # pragma: no cover
        print(f"OCR: NOT available ({exc})")
    print(f"Captures: {privacy.pause_state().describe()}; data in {home()}")
    print(f"Snips folder: {screenshots_dir()}")
    print(f"Tray app: {'running' if tray_running() else 'not running'}; startup shortcut: {'yes' if STARTUP_LINK.exists() else 'no'}")
    for problem in s.problems:
        print(f"Config problem: {problem}")
    for client in CLIENTS:
        if client.detect():
            print(f"{client.name}: {client.check()}")
    return 0
