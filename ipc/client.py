"""Client for the Live Activity API.

Pure standard library on purpose. A producer is usually a build script or a
test hook, and making it import PySide6 to report a percentage would be an
absurd cost. This module imports nothing beyond ``socket`` and ``json``, so
dropping it into another project is free.

Typical use from Python::

    from ipc.client import activity

    with activity("build", "docker build · api", glyph="cube") as act:
        for step, total in steps():
            act.update(progress=step / total, detail=f"step {step} of {total}")

The context manager reports success on a clean exit and failure if the
block raises, so a crashed build shows as failed rather than hanging on
screen until its time-to-live expires.
"""

from __future__ import annotations

import contextlib
import json
import socket
from types import TracebackType
from typing import Any

from ipc import endpoint as endpoint_module
from ipc import protocol

DEFAULT_TIMEOUT = 2.0


class ActivityError(RuntimeError):
    """The island refused a command or could not be reached."""


class IslandClient:
    """A short-lived connection to the island."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._endpoint = endpoint_module.discover()
        self._socket: socket.socket | None = None
        self._reader: Any = None

    # ---- connection --------------------------------------------------

    @property
    def available(self) -> bool:
        return self._endpoint is not None

    def connect(self) -> None:
        if self._socket is not None:
            return
        if self._endpoint is None:
            raise ActivityError(
                "the island is not running (no endpoint file found)"
            )
        try:
            sock = socket.create_connection(
                (self._endpoint.host, self._endpoint.port), self._timeout
            )
        except OSError as exc:
            raise ActivityError(f"cannot reach the island: {exc}") from exc
        sock.settimeout(self._timeout)
        self._socket = sock
        self._reader = sock.makefile("rb")

    def close(self) -> None:
        if self._reader is not None:
            with contextlib.suppress(OSError):
                self._reader.close()
            self._reader = None
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None

    def __enter__(self) -> IslandClient:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- commands ----------------------------------------------------

    def send(self, **fields: Any) -> dict[str, Any]:
        """Send one command and return the decoded response."""
        self.connect()
        assert self._socket is not None and self._endpoint is not None
        fields["token"] = self._endpoint.token
        try:
            self._socket.sendall(protocol.encode_command(fields))
            line = self._reader.readline()
        except OSError as exc:
            raise ActivityError(f"connection lost: {exc}") from exc
        if not line:
            raise ActivityError("the island closed the connection")
        try:
            response = json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise ActivityError(f"malformed response: {exc}") from exc
        if not response.get("ok"):
            raise ActivityError(str(response.get("error", "refused")))
        return response

    def ping(self) -> dict[str, Any]:
        return self.send(op=protocol.OP_PING)

    def list(self) -> list[str]:
        return list(self.send(op=protocol.OP_LIST).get("activities", []))

    def push(self, activity_id: str, title: str, **fields: Any) -> dict[str, Any]:
        return self.send(
            op=protocol.OP_PUSH, id=activity_id, title=title, **fields
        )

    def done(self, activity_id: str, status: str = "ok", **fields: Any):
        return self.send(
            op=protocol.OP_DONE, id=activity_id, status=status, **fields
        )

    def retract(self, activity_id: str) -> dict[str, Any]:
        return self.send(op=protocol.OP_RETRACT, id=activity_id)


class Activity:
    """One live activity, scoped to a ``with`` block."""

    def __init__(
        self,
        activity_id: str,
        title: str,
        glyph: str | None = None,
        detail: str | None = None,
        progress: float | None = -1.0,
        quiet: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.id = activity_id
        self.title = title
        self.glyph = glyph
        self._quiet = quiet
        self._client = IslandClient(timeout=timeout)
        self._initial = {"detail": detail, "progress": progress}
        self._live = False

    def _guard(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Run a call, swallowing errors when ``quiet``.

        A producer should not fail its own build because the island is not
        running. Quiet mode is the default for exactly that reason.
        """
        try:
            return fn(*args, **kwargs)
        except ActivityError:
            if not self._quiet:
                raise
            self._live = False
            return None

    def start(self) -> Activity:
        result = self._guard(
            self._client.push,
            self.id,
            self.title,
            glyph=self.glyph,
            **{k: v for k, v in self._initial.items() if v is not None},
        )
        self._live = result is not None
        return self

    def update(
        self,
        progress: float | None = None,
        detail: str | None = None,
        title: str | None = None,
        urgency: str | None = None,
    ) -> None:
        if not self._live:
            return
        fields: dict[str, Any] = {}
        if progress is not None:
            fields["progress"] = progress
        if detail is not None:
            fields["detail"] = detail
        if urgency is not None:
            fields["urgency"] = urgency
        self._guard(
            self._client.push,
            self.id,
            title or self.title,
            glyph=self.glyph,
            **fields,
        )

    def finish(
        self,
        status: str = "ok",
        title: str | None = None,
        detail: str | None = None,
    ) -> None:
        if not self._live:
            return
        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
        if detail:
            fields["detail"] = detail
        self._guard(self._client.done, self.id, status, **fields)
        self._live = False

    def __enter__(self) -> Activity:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> bool:
        if exc_type is None:
            self.finish("ok")
        elif isinstance(exc, KeyboardInterrupt):
            self.finish("cancelled")
        else:
            self.finish("fail", detail=str(exc)[:160] or None)
        self._client.close()
        return False  # never swallow the producer's exception


def activity(
    activity_id: str,
    title: str,
    glyph: str | None = None,
    detail: str | None = None,
    progress: float | None = -1.0,
    quiet: bool = True,
) -> Activity:
    """Convenience constructor, see the module docstring."""
    return Activity(
        activity_id,
        title,
        glyph=glyph,
        detail=detail,
        progress=progress,
        quiet=quiet,
    )
