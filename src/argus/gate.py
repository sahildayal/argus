"""The MCP disclosure decision boundary. The tray owns all cross-call trust."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import privacy
from .config import config_path, file_revision, settings
from .privacy import WindowSet

Stamp = tuple[tuple[int, int, int], tuple[int, int, int]]
Reason = Literal["user", "trusted", "timeout", "tray_unavailable", "denied", "not_required"]
DECLINED = "The user declined to share this capture."
UNAVAILABLE = "Approval is unavailable. Ask the user to start or check the Argus tray (argus-tray), then try again."
QUERY_LABEL = "Requested by the AI"


@dataclass(frozen=True)
class Policy:
    stamp: Stamp
    enabled: bool
    paused: bool
    timeout: float
    trust_minutes: float


def policy() -> Policy:
    def stamp() -> Stamp:
        return file_revision(config_path()), file_revision(privacy.pause_revision_file())

    settings()  # first use can create the config file
    before = stamp()
    s = settings()
    paused = privacy.pause_state().paused
    if before != stamp():
        raise GateStopped(UNAVAILABLE)
    return Policy(before, s.requires_approval, paused, s.gate_timeout_seconds, s.gate_trust_minutes)


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: Reason
    tool: str
    apps: list[str]
    when: float
    request_id: str


@dataclass(frozen=True)
class Preview:
    request_id: str
    tool: str
    images: tuple[bytes, ...]
    text: tuple[str, ...]
    windows: WindowSet | None  # an unknown source (snip, old capture) cannot be trusted
    stamp: Stamp
    deadline: float
    allow_trust: bool
    watching: bool
    query: str = ""


class GateStopped(Exception):
    """Stops an MCP call without exposing its partial result."""


def require(decision: Decision) -> None:
    if not decision.approved:
        raise GateStopped(UNAVAILABLE if decision.reason == "tray_unavailable" else DECLINED)


def _transport(preview: Preview) -> str:
    from .gate_pipe import exchange

    return exchange(preview)


def request(
    tool: str,
    shots: tuple[bytes, ...] = (),
    text: tuple[str, ...] = (),
    *,
    windows: WindowSet | None = None,
    start: Policy | None = None,
    required: bool = True,
    previously_approved: bool = False,
    allow_trust: bool = True,
    watching: bool = False,
    query: str = "",
    transport: Callable[[Preview], str] | None = None,
) -> Decision:
    """Decide whether MCP can disclose an output or use an earlier watch segment."""
    rid = uuid.uuid4().hex
    reason: Reason = "tray_unavailable"
    try:
        current = policy()
        start = start or current
        if current != start or (required and current.paused and current.enabled):
            reason = "denied"
        elif not required or not current.enabled or previously_approved:
            reason = "not_required"
        else:
            deadline = time.monotonic() + current.timeout
            preview = Preview(rid, tool, shots, text, windows, current.stamp, deadline, allow_trust, watching, query)
            reply = (transport or _transport)(preview)
            if time.monotonic() >= deadline:
                reason = "timeout"
            elif policy() != current:
                reason = "denied"
            elif reply in ("user", "denied", "timeout", "tray_unavailable"):
                reason = reply
            elif reply == "trusted":
                reason = "trusted" if allow_trust and windows else "denied"
    except TimeoutError:
        reason = "timeout"
    except (OSError, ValueError, GateStopped):
        pass  # Never include the transport error or preview content in an MCP reply.
    apps = sorted({exe for _, exe, _ in windows or () if exe})
    return Decision(reason in ("user", "trusted", "not_required"), reason, tool, apps, time.time(), rid)


class Trust:
    """Tray-local trust. A policy revision or tray restart discards the set."""

    def __init__(self) -> None:
        self.stamp: Stamp | None = None
        self.windows: WindowSet | None = None
        self.expires = 0.0

    def revoke(self) -> None:
        self.windows = None
        self.expires = 0.0

    def refresh(self, current: Policy) -> None:
        if current.stamp != self.stamp or current.paused or not current.enabled or time.monotonic() >= self.expires:
            self.revoke()

    def valid(self, preview: Preview, current: Policy) -> bool:
        return current.enabled and not current.paused and current.stamp == preview.stamp

    def covers(self, preview: Preview, current: Policy) -> bool:
        return (
            self.valid(preview, current)
            and preview.allow_trust
            and current.stamp == self.stamp
            and time.monotonic() < self.expires
            and self.windows is not None
            and bool(preview.windows)
            and preview.windows <= self.windows
        )

    def grant(self, preview: Preview, current: Policy) -> bool:
        if not (preview.allow_trust and preview.windows and self.valid(preview, current)):
            return False
        self.stamp = current.stamp
        self.windows = preview.windows
        self.expires = time.monotonic() + current.trust_minutes * 60
        return True
