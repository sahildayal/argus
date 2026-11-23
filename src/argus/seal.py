"""Authenticating the PNGs Argus writes, so a saved file can't claim to be a
work-profile capture it isn't.

Captures, marks and baselines carry their metadata (profile, policy revision,
label, ...) as JSON in a PNG text chunk, and an HMAC-SHA256 seal over that JSON
plus a hash of the pixels. The key is 32 random bytes in ``~/.argus/seal.key``,
created on first use. Anyone who can read that file can forge seals, so it
relies on the folder being private to the user: ``%USERPROFILE%`` has that ACL
by default on Windows (the user, SYSTEM and Administrators only), and the file
is created with 0600-style permissions where the OS honours them."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

from PIL import Image

from .config import home

_VERSION = b"argus-seal-v1\n"
_lock = threading.Lock()
_keys: dict[Path, bytes] = {}


def _key_path() -> Path:
    return home() / "seal.key"


def key() -> bytes:
    path = _key_path()
    with _lock:
        if path in _keys:
            return _keys[path]
        for _ in range(20):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                data = b""
            if len(data) == 32:
                _keys[path] = data
                return data
            if not data:
                _create(path)
                continue
            # Another process is mid-write (or the file is damaged): wait a bit.
            threading.Event().wait(0.05)
        raise OSError(f"{path.name} is damaged; delete it (old captures then can't be reopened in the work profile)")


def _create(path: Path) -> None:
    """Write a fresh key without ever replacing one another process made."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, secrets.token_bytes(32))
    finally:
        os.close(fd)
    try:
        os.rename(tmp, path)  # fails if it exists (Windows), so the first key wins
    except FileExistsError:
        pass
    finally:
        tmp.unlink(missing_ok=True)


def _pixels(image: Image.Image) -> bytes:
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    h = hashlib.sha256(f"{rgb.width}x{rgb.height}\n".encode())
    h.update(rgb.tobytes())
    return h.digest()


def sign(meta_json: str, image: Image.Image) -> str:
    msg = _VERSION + meta_json.encode("utf-8") + b"\n" + _pixels(image)
    return hmac.new(key(), msg, hashlib.sha256).hexdigest()


def verify(meta_json: str, image: Image.Image, seal: str) -> bool:
    if not seal:
        return False
    try:
        expected = sign(meta_json, image)
    except OSError:
        return False
    return hmac.compare_digest(expected, seal)
