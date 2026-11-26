"""Publishing and discovering the Live Activity endpoint.

The server picks a free loopback port at startup and writes it, with a
freshly generated token, to a small file in the user's roaming profile.
Clients read that file. Nothing has to agree on a port number in advance,
and a stale file from a crashed run is detected by trying to connect rather
than trusted blindly.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from core import paths
from ipc.protocol import PROTOCOL_VERSION, new_token

ENDPOINT_FILE = "endpoint.json"


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    token: str
    pid: int
    version: int = PROTOCOL_VERSION


def endpoint_path() -> Path:
    return paths.user_dir() / ENDPOINT_FILE


def publish(port: int, token: str, host: str = "127.0.0.1") -> Path:
    """Write the endpoint file atomically and return its path."""
    path = endpoint_path()
    payload = json.dumps(
        {
            "host": host,
            "port": int(port),
            "token": token,
            "pid": os.getpid(),
            "version": PROTOCOL_VERSION,
        },
        indent=2,
    )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".endpoint-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def discover() -> Endpoint | None:
    """Read the endpoint file. ``None`` when absent or unreadable."""
    try:
        raw = json.loads(endpoint_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return Endpoint(
            host=str(raw.get("host", "127.0.0.1")),
            port=int(raw["port"]),
            token=str(raw["token"]),
            pid=int(raw.get("pid", 0)),
            version=int(raw.get("version", PROTOCOL_VERSION)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear() -> None:
    """Remove the endpoint file on a clean shutdown."""
    with contextlib.suppress(OSError):
        endpoint_path().unlink()


def fresh_token() -> str:
    return new_token()
