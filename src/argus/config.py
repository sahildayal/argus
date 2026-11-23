"""Paths and user settings (``~/.argus/config.toml``), reloaded whenever the file changes."""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = """\
# Argus settings. Edits apply to the next capture - nothing needs restarting
# except the tray app for hotkey changes.

[profile]
# "personal": everything is visible except what [privacy] blocks (a block list).
# "work": an allow list - only windows allowed under [work] are visible; the
#   desktop, the taskbar and every other window are blacked out, and their
#   titles never leave Argus. [privacy] still applies on top. Use it wherever
#   screens can show confidential data (patient records, customer data, ...).
# Switch with the tray menu or `argus profile work` / `argus profile personal`.
active = "personal"

[gate]
# Approval is on in work, off in personal. Uncomment to opt in for personal too.
# enabled = true
timeout_seconds = 30
trust_minutes = 10

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

[work]
# Only used when [profile] active = "work". A window is visible only if it
# matches one of these AND isn't blocked by [privacy]. Everything else -
# including the desktop, the taskbar and windows of unknown programs - is
# blacked out, and its title is replaced by "<app> (hidden, work profile)".
# Programs whose windows are allowed (exe names, any case), e.g.
#   allow_apps = ["Code.exe", "WindowsTerminal.exe", "devenv.exe"]
allow_apps = []
# Regexes (case-insensitive) matched against browser window titles, i.e. the
# active tab's title. Switching to a tab that doesn't match hides the window
# again. e.g. allow_browser_titles = ['\\blocalhost\\b', '\\bgithub\\.com\\b']
allow_browser_titles = []
# Regexes matched against any window title (optional; prefer allow_apps).
allow_titles = []
# Let latest_snip read saved snips from Pictures\\Screenshots. Off by default:
# a snip is an image of unknown origin, so Argus can't black anything out in it.
allow_snip_folder = false
"""


def home() -> Path:
    """Argus's data folder (``ARGUS_HOME`` overrides, mainly for tests)."""
    path = Path(os.environ.get("ARGUS_HOME") or Path.home() / ".argus")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home() / "config.toml"


def tray_mutex_name() -> str:
    path = os.path.normcase(str(home().resolve()))
    default = os.path.normcase(str((Path.home() / ".argus").resolve()))
    if path == default:
        return "Local\\ArgusTray"
    return "Local\\ArgusTray-" + hashlib.sha256(path.encode()).hexdigest()


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
    profile: str = "personal"  # "personal" (block list) | "work" (allow list)
    allow_apps: frozenset[str] = frozenset()
    allow_browser_titles: tuple[re.Pattern, ...] = ()
    allow_titles: tuple[re.Pattern, ...] = ()
    allow_snip_folder: bool = False
    gate_enabled: bool = False  # personal opt-in; work always requires approval
    gate_timeout_seconds: float = 30.0
    gate_trust_minutes: float = 10.0
    problems: tuple[str, ...] = field(default=())

    @property
    def work(self) -> bool:
        return self.profile == "work"

    @property
    def requires_approval(self) -> bool:
        return self.work or self.gate_enabled

    @classmethod
    def from_toml(cls, text: str) -> Settings:
        problems: list[str] = []
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            data = tomllib.loads(DEFAULT_CONFIG)
            if _WORK_LINE.search(text):
                # Fail closed: a typo must not silently drop a work profile back
                # to personal. Work with empty allow lists hides everything.
                data["profile"]["active"] = "work"
                problems.append(
                    f"config.toml is not valid TOML ({exc}); using defaults, but staying in the work profile with nothing allowed"
                )
            else:
                problems.append(f"config.toml is not valid TOML ({exc}); using defaults")
        defaults = tomllib.loads(DEFAULT_CONFIG)
        gate = data.get("gate", {})
        if not isinstance(gate, dict):
            problems.append("gate: expected a table; using the defaults")
            gate = {}

        def get(section: str, key: str):
            return data.get(section, {}).get(key, defaults[section][key])

        def patterns(section: str, key: str) -> tuple[re.Pattern, ...]:
            out = []
            for raw in get(section, key):
                try:
                    out.append(re.compile(raw, re.IGNORECASE))
                except re.error as exc:
                    problems.append(f"{section}.{key}: bad regex {raw!r} ({exc})")
            return tuple(out)

        def gate_duration(key: str) -> float:
            value = gate.get(key, defaults["gate"][key])
            if type(value) in (int, float) and 0 < value <= 86400 and math.isfinite(value):
                return float(value)
            problems.append(f"gate.{key}: expected a positive number up to 86400; using the default")
            return float(defaults["gate"][key])

        profile = str(get("profile", "active")).strip().lower()
        if profile not in PROFILES:
            problems.append(f'profile.active: unknown profile {profile!r} (use "personal" or "work"); using "work" to be safe')
            profile = "work"

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
            redact_browser_titles=patterns("privacy", "redact_browser_titles"),
            redact_titles=patterns("privacy", "redact_titles"),
            browsers=frozenset(b.lower() for b in get("privacy", "browsers")),
            profile=profile,
            allow_apps=frozenset(a.lower() for a in get("work", "allow_apps")),
            allow_browser_titles=patterns("work", "allow_browser_titles"),
            allow_titles=patterns("work", "allow_titles"),
            allow_snip_folder=bool(get("work", "allow_snip_folder")),
            gate_enabled=gate.get("enabled") is True,
            gate_timeout_seconds=gate_duration("timeout_seconds"),
            gate_trust_minutes=gate_duration("trust_minutes"),
            problems=tuple(problems),
        )


PROFILES = ("personal", "work")
_WORK_LINE = re.compile(r"""^\s*active\s*=\s*["']work["']""", re.IGNORECASE | re.MULTILINE)


_lock = threading.Lock()
_cache: tuple[tuple[Path, int, int, int], Settings] | None = None


def file_revision(path: Path) -> tuple[int, int, int]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (0, 0, 0)
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def settings() -> Settings:
    """Current settings; writes the commented default file on first use."""
    global _cache
    path = config_path()
    with _lock:
        if not path.exists():
            path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        revision = (path, *file_revision(path))
        if _cache is None or _cache[0] != revision:
            _cache = (revision, Settings.from_toml(path.read_text(encoding="utf-8")))
        return _cache[1]


def set_profile(name: str) -> Settings:
    """Switch ``[profile] active`` in config.toml, editing just that line so the
    user's comments and layout survive. Returns the new settings."""
    global _cache
    name = name.strip().lower()
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; use one of: {', '.join(PROFILES)}")
    settings()  # writes the default file if there is none
    path = config_path()
    with _lock:
        with open(path, encoding="utf-8", newline="") as f:  # keep the file's line endings
            text = f.read()
        new = _with_profile(text, name)
        try:
            check = tomllib.loads(new).get("profile", {}).get("active")
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} isn't valid TOML ({exc}); fix it first, then switch profiles") from None
        if check != name:
            raise ValueError(f"couldn't update [profile] active in {path}; edit it by hand")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)
        _cache = None
    return settings()


_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_ACTIVE = re.compile(r"""^(\s*active\s*=\s*)("[^"]*"|'[^']*'|[^\s#]+)(.*)$""")


def _with_profile(text: str, name: str) -> str:
    """*text* with ``[profile] active`` set to *name*; nothing else changes."""
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if (m := _SECTION.match(line)) and m.group(1).strip() == "profile"), None)
    if header is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        return f'{text}{sep}\n[profile]\nactive = "{name}"\n'
    for i in range(header + 1, len(lines)):
        if _SECTION.match(lines[i]):
            break
        body = lines[i].rstrip("\r\n")
        m = _ACTIVE.match(body)
        if m:
            lines[i] = f'{m.group(1)}"{name}"{m.group(3)}{lines[i][len(body):]}'
            return "".join(lines)
    newline = "\r\n" if lines[header].endswith("\r\n") else "\n"
    if not lines[header].endswith("\n"):
        lines[header] += newline
    lines.insert(header + 1, f'active = "{name}"{newline}')
    return "".join(lines)


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
