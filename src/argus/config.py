"""Paths and user settings (``~/.argus/config.toml``), reloaded whenever the file changes."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from functools import cached_property
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

[audit]
# Audit logging is on in work, off in personal. Uncomment to opt in for personal too.
# enabled = true
retention_months = 12

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
# again. NOT a security boundary: a page sets its own title, so any website
# can call itself "localhost". For strict control, use a separate browser (or
# browser exe) only for work tools and put that in allow_apps instead.
allow_browser_titles = []
# Regexes matched against any window title (a convenience, not a boundary:
# programs choose their own titles; prefer allow_apps).
allow_titles = []
# Let latest_snip read saved snips from Pictures\\Screenshots. Off by default:
# a snip is an image of unknown origin, so Argus can't black anything out in it.
allow_snip_folder = false
# Let latest_snip open image files copied in Explorer (a clipboard file list).
# Off by default: a copied file can be any image on disk. Separate from
# allow_snip_folder on purpose.
allow_clipboard_files = false
"""

# Bump whenever privacy.py's masking changes (what gets revealed or hidden), so
# captures sealed under the old masker stop replaying in the work profile.
REDACTION_VERSION = 2


def home() -> Path:
    """Argus's data folder (``ARGUS_HOME`` overrides, mainly for tests)."""
    path = Path(os.environ.get("ARGUS_HOME") or Path.home() / ".argus")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return home() / "config.toml"


def profile_state_path() -> Path:
    """Remembers that the work profile was switched on (``work``) or explicitly
    off (``personal``), so losing or mangling config.toml can't drop work."""
    return home() / "profile.state"


def read_profile_state() -> str | None:
    """"work", "personal", or None when there's no marker (first run). A marker
    that exists but can't be read or says anything else counts as work."""
    path = profile_state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return "work"
    return "personal" if text.strip().lower() == "personal" else "work"


def write_profile_state(name: str) -> None:
    path = profile_state_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, path)


def tray_mutex_name() -> str:
    path = os.path.normcase(str(home().resolve()))
    default = os.path.normcase(str((Path.home() / ".argus").resolve()))
    if path == default:
        return "Local\\ArgusTray"
    return "Local\\ArgusTray-" + hashlib.sha256(path.encode()).hexdigest()
# What config.toml may contain. STRS = a list of strings; NUMBER = int or float.
STRS, NUMBER = "list of strings", "number"
_SCHEMA: dict[str, dict[str, object]] = {
    "profile": {"active": str},
    "capture": {"max_side": int, "max_side_multi": int, "grid": bool, "cursor_marker": bool},
    "look": {"mark_freshness_seconds": NUMBER},
    "hotkeys": {"mark": str, "pause": str},
    "privacy": {
        "retention_days": NUMBER, "redact_notifications": bool, "redact_apps": STRS,
        "redact_browser_titles": STRS, "redact_titles": STRS, "browsers": STRS,
    },
    "work": {
        "allow_apps": STRS, "allow_browser_titles": STRS, "allow_titles": STRS, "allow_snip_folder": bool,
        "allow_clipboard_files": bool,
    },
    "gate": {"enabled": bool, "timeout_seconds": NUMBER, "trust_minutes": NUMBER},
    "audit": {"enabled": bool, "retention_months": int},
}
# Sections whose mistakes can weaken the work profile: any problem in them locks
# the work profile down to "nothing allowed" instead of half-applying the file.
_CRITICAL = {"profile", "privacy", "work"}
# An unknown section this close (edit distance) to one of these is taken for a
# misspelling of it - e.g. [profiles] or [privcy] - and counts as a critical problem.
_TYPO_TARGETS = ("profile", "privacy", "work", "gate")
_TYPO_DISTANCE = 2


def _distance(a: str, b: str) -> int:
    """Edit distance counting an adjacent swap ("wrok") as one edit (optimal string alignment)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _typo_of(section: str) -> str | None:
    name = section.strip().lower()
    # Short names ("work", "gate") only tolerate one edit, or unrelated sections like [data] would match.
    hits = [(d, t) for t in _TYPO_TARGETS
            if (d := _distance(name, t)) <= (1 if len(t) <= 4 else _TYPO_DISTANCE)]
    return min(hits)[1] if hits else None


def _type_ok(value: object, kind: object) -> bool:
    if kind is bool:
        return type(value) is bool
    if kind is int:
        return type(value) is int
    if kind is NUMBER:
        return type(value) in (int, float)
    if kind is str:
        return type(value) is str
    if kind is STRS:
        return type(value) is list and all(type(v) is str for v in value)
    return False


def _kind_name(kind: object) -> str:
    names = {bool: "true or false", int: "a whole number", str: "a quoted string"}
    return names.get(kind, f"a {kind}")  # type: ignore[call-overload]


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
    allow_clipboard_files: bool = False
    gate_enabled: bool = False  # personal opt-in; work always requires approval
    gate_timeout_seconds: float = 30.0
    gate_trust_minutes: float = 10.0
    audit_enabled: bool = False  # personal opt-in; work always logs audit
    audit_retention_months: int = 12
    problems: tuple[str, ...] = field(default=())
    configured: str = ""  # what a valid [profile] active says ("" = missing or unusable)
    profile_reason: str = ""  # why the work profile is on, for status / tray
    lockdown: bool = False  # the fail-closed fallback: work with nothing allowed

    @property
    def work(self) -> bool:
        return self.profile == "work"

    @property
    def requires_approval(self) -> bool:
        return self.work or self.gate_enabled
    @cached_property
    def policy(self) -> str:
        """A short hash of everything that decides what the AI may see (profile,
        allow lists, block lists). Captures are stamped with it; in the work
        profile an image is only shown again under the same policy."""
        data = {
            "redaction_version": REDACTION_VERSION,
            "profile": self.profile,
            "allow_apps": sorted(self.allow_apps),
            "allow_browser_titles": [p.pattern for p in self.allow_browser_titles],
            "allow_titles": [p.pattern for p in self.allow_titles],
            "allow_snip_folder": self.allow_snip_folder,
            "allow_clipboard_files": self.allow_clipboard_files,
            "redact_apps": sorted(self.redact_apps),
            "redact_browser_titles": [p.pattern for p in self.redact_browser_titles],
            "redact_titles": [p.pattern for p in self.redact_titles],
            "browsers": sorted(self.browsers),
            "redact_notifications": self.redact_notifications,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]

    @property
    def requires_audit(self) -> bool:
        return self.work or self.audit_enabled

    @classmethod
    def from_toml(cls, text: str, sticky_work: bool = False) -> Settings:
        """Settings from config.toml's text. *sticky_work*: the work profile was
        switched on (``profile.state`` says work), so the result is work whatever
        the file says, until it's switched off with `argus profile personal`."""
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return cls.locked(f"config.toml is not valid TOML ({exc})", sticky_work)
        return cls._from_data(data, [], broken=False, sticky=sticky_work)

    @classmethod
    def locked(cls, why: str, sticky_work: bool = False) -> Settings:
        """Settings for a config file Argus can't read: the work profile with
        nothing allowed. Failing closed matters more than a personal-profile user
        seeing black captures until they fix the file (the problem says why)."""
        problem = f"{why}. Until it's fixed, Argus uses the work profile with nothing allowed."
        return cls._from_data({}, [problem], broken=True, sticky=sticky_work)

    @classmethod
    def _from_data(cls, data: dict, problems: list[str], broken: bool, sticky: bool = False) -> Settings:
        defaults = tomllib.loads(DEFAULT_CONFIG)
        critical: set[str] = set()  # sections with a problem
        typo = False  # a misspelt critical section (any of them locks the work profile)

        def bad(section: str, message: str) -> None:
            problems.append(message)
            critical.add(section)

        for section, value in data.items():
            if section not in _SCHEMA and (near := _typo_of(section)):
                bad(near, f"[{section}]: unknown section that looks like a misspelt [{near}]; its settings are ignored, "
                          "so it counts as a problem in that section")
                typo = True
            elif section not in _SCHEMA:
                problems.append(f"[{section}]: unknown section (ignored)")
            elif not isinstance(value, dict):
                bad(section, f"{section}: should be a [{section}] section, not a single value")
            else:
                for key in value:
                    if key not in _SCHEMA[section]:
                        bad(section, f"{section}.{key}: unknown setting (typo?)")
        gate = data.get("gate") if isinstance(data.get("gate"), dict) else {}

        audit = data.get("audit", {})
        if not isinstance(audit, dict):
            problems.append("audit: expected a table; using the defaults")
            audit = {}

        def get(section: str, key: str):
            sect = data.get(section)
            if not isinstance(sect, dict) or key not in sect:
                return defaults[section][key]
            value, kind = sect[key], _SCHEMA[section][key]
            if _type_ok(value, kind):
                return value
            # The value itself is never echoed: problems are shown to the AI.
            bad(section, f"{section}.{key}: should be {_kind_name(kind)}, not {type(value).__name__}; using the default")
            return defaults[section][key]

        def patterns(section: str, key: str) -> tuple[re.Pattern, ...]:
            out = []
            for i, raw in enumerate(get(section, key)):
                try:
                    out.append(re.compile(raw, re.IGNORECASE))
                except re.error as exc:
                    bad(section, f"{section}.{key}: entry {i + 1} is not a valid regex ({exc})")
            return tuple(out)

        def gate_duration(key: str) -> float:
            value = gate.get(key, defaults["gate"][key])
            if type(value) in (int, float) and 0 < value <= 86400 and math.isfinite(value):
                return float(value)
            problems.append(f"gate.{key}: expected a positive number up to 86400; using the default")
            return float(defaults["gate"][key])

        def audit_retention() -> int:
            val = audit.get("retention_months", defaults.get("audit", {}).get("retention_months", 12))
            if type(val) is int and val > 0:
                return val
            problems.append("audit.retention_months: expected a positive integer; using the default")
            return 12

        sect = data.get("profile")
        raw = sect.get("active", "personal") if isinstance(sect, dict) else "personal"
        profile = raw.strip().lower() if isinstance(raw, str) else ""
        if isinstance(raw, str) and profile not in PROFILES:
            bad("profile", 'profile.active: unknown profile (use "personal" or "work"); using "work" with nothing allowed to be safe')
        get("profile", "active")  # reports a non-string value
        has_active = isinstance(sect, dict) and "active" in sect
        configured = profile if has_active and profile in PROFILES and "profile" not in critical else ""

        values = dict(
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
            allow_apps=frozenset(a.lower() for a in get("work", "allow_apps")),
            allow_browser_titles=patterns("work", "allow_browser_titles"),
            allow_titles=patterns("work", "allow_titles"),
            allow_snip_folder=bool(get("work", "allow_snip_folder")),
            allow_clipboard_files=bool(get("work", "allow_clipboard_files")),
            gate_enabled=gate.get("enabled") is True,
            gate_timeout_seconds=gate_duration("timeout_seconds"),
            gate_trust_minutes=gate_duration("trust_minutes"),
            # Personal opt-in only: the work profile always audits (Settings.requires_audit).
            audit_enabled=audit.get("enabled") is True,
            audit_retention_months=audit_retention(),
        )
        # Fail closed: an unreadable file or a broken [profile] means work; a
        # mistake anywhere that shapes the work profile means nothing is allowed
        # (a half-applied file could leave an allow rule active without the
        # block rule meant to go with it). Once switched on, work stays on
        # whatever the file says (an empty, recreated or hand-edited file
        # included) until it's switched off with `argus profile personal`.
        reason = ""
        if sticky and profile != "work":
            what = {"personal": 'says active = "personal"'}.get(configured, "doesn't set [profile] active")
            if broken:
                what = "can't be used"
            reason = (
                f"the work profile was switched on (tray or `argus profile work`) and stays on until it's switched off "
                f"the same way; config.toml {what}, which alone can't turn it off"
            )
            profile = "work"
        elif broken:
            reason = "config.toml can't be used, so Argus fails closed to the work profile"
        elif "profile" in critical:
            reason = "config.toml's [profile] section has a problem, so Argus fails closed to the work profile"
        if broken or "profile" in critical:
            profile = "work"
        if not reason and profile == "work":
            reason = 'config.toml sets [profile] active = "work"'
        lockdown = profile == "work" and bool(broken or critical & _CRITICAL or typo)
        if lockdown:
            if not broken:
                problems.append("Because of the problem(s) above, the work profile allows nothing until config.toml is fixed.")
            values.update(allow_apps=frozenset(), allow_browser_titles=(), allow_titles=(), allow_snip_folder=False,
                          allow_clipboard_files=False)
        return cls(profile=profile, problems=tuple(problems), configured=configured, profile_reason=reason,
                   lockdown=lockdown, **values)


PROFILES = ("personal", "work")


_lock = threading.Lock()
_cache: tuple[tuple, Settings] | None = None


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
    marker = profile_state_path()
    with _lock:
        if not path.exists():
            path.write_text(DEFAULT_CONFIG, encoding="utf-8")
        revision = (path, *file_revision(path), *file_revision(marker))
        if _cache is None or _cache[0] != revision:
            state = read_profile_state()
            sticky = state == "work"
            try:
                parsed = Settings.from_toml(path.read_text(encoding="utf-8"), sticky_work=sticky)
            except (OSError, UnicodeDecodeError) as exc:
                parsed = Settings.locked(f"config.toml can't be read ({type(exc).__name__})", sticky)
            if parsed.configured == "work" and not sticky:
                # Work chosen by editing the file sticks too: a later edit that
                # drops or mangles [profile] mustn't quietly turn it off.
                try:
                    write_profile_state("work")
                except OSError:
                    pass
                revision = (path, *file_revision(path), *file_revision(marker))
            _cache = (revision, parsed)
        return _cache[1]


def set_profile(name: str) -> Settings:
    """Switch ``[profile] active`` in config.toml, editing just that line so the
    user's comments and layout survive. Returns the new settings. The edit is
    verified by parsing the file before and after: only profile.active may
    change, or the file is left alone.

    The choice is also recorded in ``profile.state``. Switching to work records
    it first, so work takes effect (and sticks) even if config.toml can't be
    edited; switching to personal records it only once the file says personal."""
    global _cache
    name = name.strip().lower()
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; use one of: {', '.join(PROFILES)}")
    if name == "work":
        with _lock:
            write_profile_state("work")
            _cache = None
    settings()  # writes the default file if there is none
    _set_profile_line(name)
    if name == "personal":
        with _lock:
            write_profile_state("personal")
            _cache = None
    return settings()


def _set_profile_line(name: str) -> None:
    global _cache
    path = config_path()
    with _lock:
        with open(path, encoding="utf-8", newline="") as f:  # keep the file's line endings
            text = f.read()
        try:
            old = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} isn't valid TOML ({exc}); fix it first, then switch profiles") from None
        if "profile" in old and not isinstance(old["profile"], dict):
            raise ValueError(f"{path} has a 'profile' value that isn't a [profile] section; fix it first")
        new = _with_profile(text, name)
        try:
            parsed = tomllib.loads(new)
        except tomllib.TOMLDecodeError:
            parsed = None
        expected = copy.deepcopy(old)
        expected.setdefault("profile", {})["active"] = name
        if parsed != expected:
            # e.g. a "[profile]" line inside a multi-line string: the line edit
            # would change something else. Don't guess - leave the file alone.
            raise ValueError(f'couldn\'t safely update [profile] active in {path}; set it to "{name}" by hand')
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)
        _cache = None


WORK_LIST_KEYS = {"allow_apps", "allow_browser_titles", "allow_titles"}


def set_work_list(key: str, values) -> Settings:
    """Atomically replace one [work] array while preserving all other TOML text."""
    global _cache
    if key not in WORK_LIST_KEYS:
        raise ValueError(f"unknown work list {key!r}")
    values = list(dict.fromkeys(str(v) for v in values))
    if key == "allow_apps":
        if any(not v.lower().endswith(".exe") for v in values):
            raise ValueError("app names must end with .exe")
        values = list({v.lower(): v for v in values}.values())
    else:
        for value in values:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex {value!r}: {exc}") from None
    settings()
    path = config_path()
    with _lock:
        with open(path, encoding="utf-8", newline="") as f:
            original = f.read()
        new = _with_work_list(original, key, values)
        try:
            before, parsed = tomllib.loads(original), tomllib.loads(new)
            result = Settings.from_toml(new)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path} isn't valid TOML ({exc}); fix it first") from None
        # Nothing but [work] <key> may change.
        for doc in (before, parsed):
            doc.get("work", {}).pop(key, None)
            if doc.get("work") == {}:
                doc.pop("work")
        if before != parsed:
            raise ValueError(f"editing [work] {key} would change other settings in {path}; edit it by hand")
        expected = {"allow_apps": result.allow_apps,
                    "allow_browser_titles": tuple(p.pattern for p in result.allow_browser_titles),
                    "allow_titles": tuple(p.pattern for p in result.allow_titles)}[key]
        expected_values = frozenset(v.lower() for v in values) if key == "allow_apps" else tuple(values)
        if expected != expected_values:
            raise ValueError(f"couldn't verify [work] {key} in {path}")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, path)
        _cache = None
    return settings()


def _with_work_list(text: str, key: str, values: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    header = next((i for i, line in enumerate(lines) if (m := _SECTION.match(line)) and m.group(1).strip() == "work"), None)
    encoded = "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"
    if header is None:
        sep = "" if not text or text.endswith("\n") else newline
        return f"{text}{sep}{newline}[work]{newline}{key} = {encoded}{newline}"
    end = next((i for i in range(header + 1, len(lines)) if _SECTION.match(lines[i])), len(lines))
    matcher = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(\[)")
    for i in range(header + 1, end):
        body = lines[i].rstrip("\r\n")
        match = matcher.match(body)
        if not match:
            continue
        # Arrays may span lines. Locate the matching bracket while respecting quoted strings.
        depth, quote, escaped, finish = 1, None, False, None
        comments = []  # comments inside the array, found outside quoted strings
        for j in range(i, end):
            row = lines[j].rstrip("\r\n")
            start = match.end(2) if j == i else 0
            for k in range(start, len(row)):
                ch = row[k]
                if quote:
                    if escaped: escaped = False
                    elif ch == "\\" and quote == '"': escaped = True
                    elif ch == quote: quote = None
                elif ch == "#":
                    comments.append(row[k:])
                    break
                elif ch in "\"'": quote = ch
                elif ch == "[": depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        finish = (j, k + 1)
                        break
            if finish: break
        if not finish: raise ValueError(f"unterminated [work] {key} array")
        j, k = finish
        suffix = lines[j].rstrip("\r\n")[k:]
        replacement = [body[:match.start(2)] + encoded + suffix + (lines[j][len(lines[j].rstrip("\r\n")):])]
        replacement.extend(f"# {comment.lstrip('# ').rstrip()}{newline}" for comment in comments)
        lines[i:j + 1] = replacement
        return "".join(lines)
    if end > header + 1 and not lines[end - 1].endswith("\n"):
        lines[end - 1] += newline
    lines.insert(end, f"{key} = {encoded}{newline}")
    return "".join(lines)


_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_ACTIVE = re.compile(r"""^(\s*active\s*=\s*)("[^"]*"|'[^']*'|[^\s#]+)(.*)$""")


def _with_profile(text: str, name: str) -> str:
    """*text* with ``[profile] active`` set to *name*; nothing else changes."""
    lines = text.splitlines(keepends=True)
    eol = "\r\n" if "\r\n" in text else "\n"  # the file's own line ending
    header = next((i for i, line in enumerate(lines) if (m := _SECTION.match(line)) and m.group(1).strip() == "profile"), None)
    if header is None:
        sep = "" if not text or text.endswith("\n") else eol
        return f'{text}{sep}{eol}[profile]{eol}active = "{name}"{eol}'
    for i in range(header + 1, len(lines)):
        if _SECTION.match(lines[i]):
            break
        body = lines[i].rstrip("\r\n")
        m = _ACTIVE.match(body)
        if m:
            lines[i] = f'{m.group(1)}"{name}"{m.group(3)}{lines[i][len(body):]}'
            return "".join(lines)
    newline = "\r\n" if lines[header].endswith("\r\n") else eol
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
