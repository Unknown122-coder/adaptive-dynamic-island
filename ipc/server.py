"""The Live Activity server.

This is the feature that has no equivalent on Windows. iOS live activities
are the actual purpose of the Dynamic Island, and every Windows clone has
reproduced the shape while leaving out the idea: an application telling the
island what it is doing, right now, with progress.

A build, a test run, a file transfer, a deploy or a download opens a socket,
pushes updates under an id, and marks it done. The island shows it, ranks it
against everything else through the arbiter, and drops it when it finishes
or when the producer goes silent.

Robustness matters more than features here, because the producers are other
people's scripts:

* A crashed producer cannot pin content on screen. Every push carries a
  time-to-live and the arbiter sweeps expired content automatically.
* A misbehaving client cannot stall the island. Reads are capped per line
  and the server is non-blocking on the Qt event loop.
* A finished activity lingers for a moment so the user actually sees the
  result, then retracts itself.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from core.arbiter import ContentArbiter, ProviderHandle
from core.config import Settings
from core.model import IslandContent, Priority, Urgency
from ipc import endpoint as endpoint_module
from ipc import protocol

_URGENCY_BY_NAME = {
    "neutral": Urgency.NEUTRAL,
    "info": Urgency.INFO,
    "success": Urgency.SUCCESS,
    "warning": Urgency.WARNING,
    "critical": Urgency.CRITICAL,
}

_STATUS_URGENCY = {
    "ok": Urgency.SUCCESS,
    "fail": Urgency.CRITICAL,
    "cancelled": Urgency.WARNING,
}

_STATUS_GLYPH = {"ok": "sparkle", "fail": "warning", "cancelled": "warning"}


class ActivityServer(QObject):
    """Accepts activity updates and publishes them into the arbiter."""

    activity_changed = Signal(str, str)  # (activity id, op)
    started = Signal(int)                # port

    def __init__(
        self,
        arbiter: ContentArbiter,
        settings: Settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "activity")
        self._settings = settings
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._buffers: dict[QTcpSocket, bytearray] = {}
        self._token = ""
        self._port = 0
        self._seen: dict[str, float] = {}

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> bool:
        """Bind to a free loopback port and publish the endpoint file."""
        if self._server.isListening():
            return True
        # Port 0 asks the OS for any free port, which avoids both a
        # hard-coded collision and a scan.
        if not self._server.listen(QHostAddress("127.0.0.1"), 0):
            print(f"[activity] cannot listen: {self._server.errorString()}")
            return False
        self._port = int(self._server.serverPort())
        self._token = endpoint_module.fresh_token()
        endpoint_module.publish(self._port, self._token)
        self.started.emit(self._port)
        return True

    def stop(self) -> None:
        for socket in list(self._buffers):
            socket.disconnectFromHost()
        self._buffers.clear()
        if self._server.isListening():
            self._server.close()
        endpoint_module.clear()
        self._handle.clear()
        self._seen.clear()

    @property
    def port(self) -> int:
        return self._port

    @property
    def listening(self) -> bool:
        return self._server.isListening()

    def active_ids(self) -> list[str]:
        return sorted(self._seen)

    # ---- connections -------------------------------------------------

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._on_ready(s))
            socket.disconnected.connect(lambda s=socket: self._on_closed(s))

    def _on_closed(self, socket: QTcpSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()

    def _on_ready(self, socket: QTcpSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))

        while True:
            index = buffer.find(b"\n")
            if index < 0:
                # Guard against a client that never sends a newline.
                if len(buffer) > protocol.MAX_LINE_BYTES:
                    self._reply(socket, protocol.error("command too large"))
                    socket.disconnectFromHost()
                    buffer.clear()
                return
            line = bytes(buffer[:index])
            del buffer[: index + 1]
            self._reply(socket, self._handle_line(line))

    @staticmethod
    def _reply(socket: QTcpSocket, payload: str) -> None:
        if socket.state() == QTcpSocket.SocketState.ConnectedState:
            socket.write((payload + "\n").encode("utf-8"))
            socket.flush()

    # ---- command handling --------------------------------------------

    def _handle_line(self, line: bytes) -> str:
        try:
            command = protocol.parse_command(line)
        except protocol.ProtocolError as exc:
            return protocol.error(str(exc))

        if not self._token or command.token != self._token:
            return protocol.error("unauthorised")

        if command.op == protocol.OP_PING:
            return protocol.ok(pong=True, port=self._port)
        if command.op == protocol.OP_LIST:
            return protocol.ok(activities=self.active_ids())
        if command.op == protocol.OP_PUSH:
            return self._do_push(command)
        if command.op == protocol.OP_DONE:
            return self._do_done(command)
        if command.op == protocol.OP_RETRACT:
            return self._do_retract(command)
        return protocol.error(f"unhandled op {command.op!r}")

    def _do_push(self, command: protocol.Command) -> str:
        ttl = command.ttl or float(
            self._settings.get("activities.stale_seconds", 90)
        )
        urgency = _URGENCY_BY_NAME.get(
            command.urgency or "neutral", Urgency.NEUTRAL
        )
        content = IslandContent(
            key=self._handle.key(command.id),
            priority=Priority.ACTIVITY,
            title=command.title or command.id,
            detail=command.detail,
            glyph=command.glyph or "pulse",
            progress=command.progress,
            urgency=urgency,
            collapsed=self._collapsed_label(command),
            expires_at=time.time() + ttl,
        )
        self._handle.publish_content(content)
        self._seen[command.id] = time.time()
        self.activity_changed.emit(command.id, protocol.OP_PUSH)
        return protocol.ok(id=command.id, ttl=ttl)

    def _do_done(self, command: protocol.Command) -> str:
        status = command.status or "ok"
        linger = float(
            self._settings.get("activities.completed_linger_seconds", 6)
        )
        content = IslandContent(
            key=self._handle.key(command.id),
            priority=Priority.ACTIVITY,
            title=command.title or f"{command.id} {self._verb(status)}",
            detail=command.detail,
            glyph=command.glyph or _STATUS_GLYPH.get(status, "sparkle"),
            progress=1.0 if status == "ok" else None,
            urgency=_STATUS_URGENCY.get(status, Urgency.SUCCESS),
            expires_at=time.time() + linger,
        )
        self._handle.publish_content(content)
        self._seen.pop(command.id, None)
        self.activity_changed.emit(command.id, protocol.OP_DONE)
        return protocol.ok(id=command.id, status=status, linger=linger)

    def _do_retract(self, command: protocol.Command) -> str:
        self._handle.retract(command.id)
        self._seen.pop(command.id, None)
        self.activity_changed.emit(command.id, protocol.OP_RETRACT)
        return protocol.ok(id=command.id)

    @staticmethod
    def _verb(status: str) -> str:
        return {"ok": "finished", "fail": "failed", "cancelled": "cancelled"}[
            status
        ]

    @staticmethod
    def _collapsed_label(command: protocol.Command) -> str:
        title = command.title or command.id
        if command.progress is not None and command.progress >= 0:
            return f"{title} {int(round(command.progress * 100))}%"
        return title
