"""Paths and user settings (``~/.argus/config.toml``), reloaded whenever the file changes."""

from __future__ import annotations

import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = """\
# Argus settings. Edits apply to the next capture - nothing needs restarting
# except the tray app for hotkey changes.

[capture]
# Longest side (px) of images sent to the AI. 2000 keeps long sessions with
# many screenshots inside the API's multi-image limit, and lets a 1080p
# monitor through at full resolution.
max_side = 2000
# Per-image size when several monitors come back at once (monitor="all").
max_side_multi = 1280
# Draw a labeled grid on every capture by default (the AI can also ask per call).
grid = false
# Mark the mouse pointer with a ring on captures that contain it.
cursor_marker = true

[look]
# A hotkey mark newer than this (seconds) wins over auto-detection in look().
mark_freshness_seconds = 120

[hotkeys]
# Used by the tray app (argus-tray). Modifiers: ctrl, alt, shift, win.
mark = "ctrl+alt+s"
pause = "ctrl+alt+p"

[privacy]
# Saved captures and marks are deleted after this many days. Baselines are kept.
retention_days = 7
# Black out Windows notification toasts (they often preview messages).
redact_notifications = true
# Windows of these programs are always blacked out (exe names, any case).
# Slack, Discord and Teams are left out on purpose - add them if you want.
redact_apps = [
  "1Password.exe", "Bitwarden.exe", "KeePass.exe", "KeePassXC.exe", "Dashlane.exe",
  "LastPass.exe", "NordPass.exe", "ProtonPass.exe", "Proton Pass.exe", "Enpass.exe",
  "RoboForm.exe", "keeperpasswordmanager.exe", "Authy Desktop.exe",
  "WhatsApp.exe", "WhatsApp.Root.exe", "Signal.exe", "Telegram.exe", "Messenger.exe",
  "PhoneExperienceHost.exe", "Viber.exe",
]
# Regexes (case-insensitive) matched against browser window titles - that is,
# the title of the active tab.
redact_browser_titles = [
  '\\bwhatsapp\\b', '\\bmessenger\\b', '\\bmessages for web\\b', '\\bgoogle messages\\b',
  '\\btelegram web\\b', '\\binstagram\\b.*\\b(direct|chats?|messages)\\b',
  '\\bbank(ing)?\\b', '\\bcredit union\\b', '\\bchase\\b', '\\bwells fargo\\b',
  '\\bcapital one\\b', '\\bciti(bank)?\\b', '\\bamerican express\\b', '\\bamex\\b',
  '\\bpaypal\\b', '\\bvenmo\\b', '\\bzelle\\b', '\\bcash app\\b', '\\brobinhood\\b',
  '\\bfidelity\\b', '\\bschwab\\b', '\\bvanguard\\b', '\\bcoinbase\\b', '\\bbinance\\b',
  '\\bsofi\\b', '\\bturbotax\\b',
]
# Regexes matched against every window title.
redact_titles = [
  '\\b1password\\b', '\\bbitwarden\\b', '\\blastpass\\b', '\\bkeepass(xc)?\\b',
  '\\bdashlane\\b', '\\bnordpass\\b', '\\bproton pass\\b',
]
browsers = [
  "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe",
  "arc.exe", "zen.exe", "librewolf.exe", "waterfox.exe", "floorp.exe", "chromium.exe",
  "thorium.exe", "comet.exe", "dia.exe", "iexplore.exe",
]
"""


def home() -> Path:
    """Argus's data folder (``ARGUS_HOME`` overrides, mainly for tests)."""
    path = Path(os.environ.get("ARGUS_HOME") or Path.home() / ".argus")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home() / "config.toml"


@dataclass(frozen=True)
class Settings:
    max_side: int = 2000
    max_side_multi: int = 1280
    grid: bool = False
    cursor_marker: bool = True
    mark_freshness: float = 120.0
    hotkey_mark: str = "ctrl+alt+s"
    hotkey_pause: str = "ctrl+alt+p"
    retention_days: float = 7.0
    redact_notifications: bool = True
    redact_apps: frozenset[str] = frozenset()
    redact_browser_titles: tuple[re.Pattern, ...] = ()
    redact_titles: tuple[re.Pattern, ...] = ()
    browsers: frozenset[str] = frozenset()
    problems: tuple[str, ...] = field(default=())

    @classmethod
    def from_toml(cls, text: str) -> Settings:
        problems: list[str] = []
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"config.toml is not valid TOML ({exc}); using defaults")
            data = tomllib.loads(DEFAULT_CONFIG)
        defaults = tomllib.loads(DEFAULT_CONFIG)

        def get(section: str, key: str):
            return data.get(section, {}).get(key, defaults[section][key])

        def patterns(key: str) -> tuple[re.Pattern, ...]:
            out = []
            for raw in get("privacy", key):
                try:
                    out.append(re.compile(raw, re.IGNORECASE))
                except re.error as exc:
                    problems.append(f"privacy.{key}: bad regex {raw!r} ({exc})")
            return tuple(out)

        return cls(
            max_side=max(256, int(get("capture", "max_side"))),
            max_side_multi=max(256, int(get("capture", "max_side_multi"))),
            grid=bool(get("capture", "grid")),
            cursor_marker=bool(get("capture", "cursor_marker")),
            mark_freshness=float(get("look", "mark_freshness_seconds")),
            hotkey_mark=str(get("hotkeys", "mark")),
            hotkey_pause=str(get("hotkeys", "pause")),
            retention_days=float(get("privacy", "retention_days")),
            redact_notifications=bool(get("privacy", "redact_notifications")),
            redact_apps=frozenset(a.lower() for a in get("privacy", "redact_apps")),
            redact_browser_titles=patterns("redact_browser_titles"),
            redact_titles=patterns("redact_titles"),
            browsers=frozenset(b.lower() for b in get("privacy", "browsers")),
            problems=tuple(problems),
        )


_lock = threading.Lock()
_cache: tuple[float, Settings] | None = None


def settings() -> Settings:
    """Current settings; writes the commented default file on first use."""
    global _cache
    path = config_path()
    with _lock:
        if not path.exists():
            path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        mtime = path.stat().st_mtime
        if _cache is None or _cache[0] != mtime:
            _cache = (mtime, Settings.from_toml(path.read_text(encoding="utf-8")))
        return _cache[1]


# ------------------------------------------------------------------- hotkeys

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN}
_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "pause": 0x13,
    "printscreen": 0x2C, "prtsc": 0x2C, "scrolllock": 0x91, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+s' -> (modifier flags incl. MOD_NOREPEAT, virtual-key code)."""
    parts = [p.strip().lower() for p in spec.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey {spec!r}")
    mods, key = 0, parts[-1]
    for part in parts[:-1]:
        if part not in _MODS:
            raise ValueError(f"unknown modifier {part!r} in hotkey {spec!r}")
        mods |= _MODS[part]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    elif key in _KEYS:
        vk = _KEYS[key]
    else:
        raise ValueError(f"unknown key {key!r} in hotkey {spec!r}")
    if not mods and vk not in (0x13, 0x2C, 0x91) and not 0x70 <= vk <= 0x87:
        raise ValueError(f"hotkey {spec!r} needs a modifier (ctrl/alt/shift/win)")
    return mods | MOD_NOREPEAT, vk


def pretty_hotkey(spec: str) -> str:
    return "+".join(p.strip().capitalize() if len(p.strip()) > 1 else p.strip().upper() for p in spec.split("+"))
