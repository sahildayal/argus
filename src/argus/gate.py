"""The disclosure decision boundary. The tray owns all cross-call trust."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from . import privacy
from .config import config_path, file_revision, profile_state_path, read_profile_state, settings
from .privacy import WindowSet

Revision = tuple[int, int, int]
Stamp = tuple[Revision, Revision, Revision, str, str]
Reason = Literal["user", "trusted", "timeout", "tray_unavailable", "denied", "not_required", "audit_failed",
                 "settings_changed"]
DECLINED = "The user declined to share this capture."
UNAVAILABLE = "Approval is unavailable. Ask the user to start or check the Argus tray (argus-tray), then try again."
QUERY_LABEL = "Requested by the AI"
AUDIT_FAILED = "The audit log could not be written."
SETTINGS_CHANGED = "Argus settings changed during the call; try again."


@dataclass(frozen=True)
class Policy:
    stamp: Stamp
    enabled: bool
    paused: bool
    timeout: float
    trust_minutes: float
    profile: str
    requires_audit: bool


def policy() -> Policy:
    def revisions() -> tuple[Revision, Revision, Revision]:
        return (file_revision(config_path()), file_revision(privacy.pause_revision_file()),
                file_revision(profile_state_path()))

    settings()  # first use can create the config file
    before = revisions()
    s = settings()
    marker = read_profile_state() or ""
    paused = privacy.pause_state().paused
    if before != revisions():
        raise GateStopped(SETTINGS_CHANGED)
    return Policy((*before, marker, s.policy), s.requires_approval, paused, s.gate_timeout_seconds, s.gate_trust_minutes,
                  s.profile, s.requires_audit)


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
        if decision.reason == "tray_unavailable":
            raise GateStopped(UNAVAILABLE)
        if decision.reason == "audit_failed":
            raise GateStopped(AUDIT_FAILED)
        if decision.reason == "settings_changed":
            raise GateStopped(SETTINGS_CHANGED)
        raise GateStopped(DECLINED)


def _transport(preview: Preview) -> str:
    from .gate_pipe import exchange

    return exchange(preview)


def policy_changed(start: Policy) -> bool:
    try:
        return policy() != start
    except (GateStopped, OSError, ValueError):
        return True


def request(
    tool: str,
    shots: tuple[bytes, ...] = (),
    text: tuple[str, ...] = (),
    *,
    windows: WindowSet | None = None,
    start: Policy | None = None,
    required: bool = True,
    allow_trust: bool = True,
    watching: bool = False,
    query: str = "",
    transport: Callable[[Preview], str] | None = None,
    client: str | None = None,
    interrupted: bool = False,
) -> Decision:
    """Decide whether to disclose an output or use an earlier watch segment."""
    rid = uuid.uuid4().hex
    reason: Reason = "tray_unavailable"
    current = None
    try:
        current = policy()
        start = start or current
        if interrupted or current != start:
            reason = "settings_changed"
        elif required and current.paused:
            reason = "denied"
        elif not required or not current.enabled:
            reason = "not_required"
        else:
            deadline = time.monotonic() + current.timeout
            preview = Preview(rid, tool, shots, text, windows, current.stamp, deadline, allow_trust, watching, query)
            reply = (transport or _transport)(preview)
            if policy_changed(current):
                reason = "settings_changed"
            elif time.monotonic() >= deadline:
                reason = "timeout"
            elif reply in ("user", "denied", "timeout", "tray_unavailable"):
                reason = reply
            elif reply == "trusted":
                reason = "trusted" if allow_trust and windows else "denied"
    except TimeoutError:
        reason = "timeout"
    except GateStopped as exc:
        if str(exc) == SETTINGS_CHANGED:
            reason = "settings_changed"
    except (OSError, ValueError):
        pass  # Never include the transport error or preview content in an MCP reply.
    if start is not None and reason in ("timeout", "tray_unavailable") and policy_changed(start):
        reason = "settings_changed"
    apps = sorted({exe for _, exe, _ in windows or () if exe})
    decision = Decision(reason in ("user", "trusted", "not_required"), reason, tool, apps, time.time(), rid)
    s = start if start is not None and start.requires_audit else current
    if s is not None and s.requires_audit:
        try:
            from . import audit

            sent_text = (*text, query) if query else text  # the AI also gets its echoed query back
            audit.log_decision(decision, shots=() if watching else shots, text=sent_text, profile=s.profile, client=client)
            if reason != "settings_changed" and start is not None and policy_changed(start):
                decision = replace(decision, approved=False, reason="settings_changed", when=time.time())
                audit.log_decision(decision, profile=s.profile, client=client)
        except Exception:
            decision = Decision(False, "audit_failed", tool, apps, decision.when, rid)
    return decision


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
