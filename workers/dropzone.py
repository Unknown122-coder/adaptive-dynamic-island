"""Drop-to-Action: the flow from a dropped file to a finished result.

The blueprint for this project proposed fixed drop zones, a left half that
compresses and a right half that converts. That design fails in practice
for two reasons: it forces the user to aim at half of a 30-pixel-tall pill
while already holding a drag, and it cannot grow past two actions.

This instead takes the drop first and then asks. Files land, the island
expands and offers only the operations that actually apply to what was
dropped, and the user clicks one. The number of actions is then free to
vary by file type, and the drop target is the whole island.

Work runs on a thread pool. OCR on a full-screen screenshot takes a couple
of seconds, and the island has to keep animating throughout, so results
come back by signal and every piece of UI work happens on the GUI thread.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QGuiApplication

from core.arbiter import ContentArbiter, ProviderHandle
from core.model import Action, IslandContent, Priority, Urgency
from workers import media

# How long an unanswered offer stays on screen. Long enough to read and
# click, short enough that a forgotten drop does not camp on the island.
OFFER_TTL = 20.0
RESULT_TTL = 14.0
# A selection larger than this is almost certainly a mis-drop.
MAX_FILES = 50


class _BatchSignals(QObject):
    progress = Signal(int, int, str)   # done, total, current file name
    finished = Signal(list)            # list[media.Result]


class _BatchTask(QRunnable):
    """Applies one operation to every dropped file, off the UI thread."""

    def __init__(self, operation: media.Operation, paths: list[Path]) -> None:
        super().__init__()
        self.operation = operation
        self.paths = paths
        self.signals = _BatchSignals()
        # Auto-delete would let Qt destroy the runnable as soon as run()
        # returns, and the signal object lives on it. Queued signals to the
        # GUI thread would then be dropped and the island would sit on
        # "working" forever. The provider owns the lifetime instead.
        self.setAutoDelete(False)

    def run(self) -> None:
        results: list[media.Result] = []
        total = len(self.paths)
        for index, path in enumerate(self.paths):
            self.signals.progress.emit(index, total, path.name)
            try:
                results.append(media.apply(self.operation, path))
            except Exception as exc:
                # One bad file must not abandon the rest of the batch.
                results.append(
                    media.Result(path, self.operation, False, f"failed: {exc}")
                )
        self.signals.progress.emit(total, total, "")
        self.signals.finished.emit(results)


class DropActionProvider(QObject):
    """Owns the drop lifecycle and publishes its states to the island."""

    # Emitted so the tray can surface a notification for long results.
    completed = Signal(str)

    def __init__(
        self,
        arbiter: ContentArbiter,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = ProviderHandle(arbiter, "drop")
        self._paths: list[Path] = []
        self._busy = False
        self._last_text: str | None = None
        self._last_output: Path | None = None
        # Holds in-flight tasks alive until their results have been
        # delivered; see the note on _BatchTask.setAutoDelete.
        self._running: set[_BatchTask] = set()

    # ---- entry point -------------------------------------------------

    def handle_drop(self, raw_paths: list[str]) -> None:
        """Called when files are dropped on the island."""
        if self._busy:
            return
        paths = self._accept(raw_paths)
        if not paths:
            self._publish_error("nothing usable was dropped")
            return
        self._paths = paths
        operations = media.operations_for(paths)
        if not operations:
            kinds = ", ".join(sorted({p.suffix.lower() or "?" for p in paths}))
            self._publish_error(f"no actions available for {kinds}")
            return
        self._publish_offer(operations)

    @staticmethod
    def _accept(raw_paths: list[str]) -> list[Path]:
        """Filter a drop down to real, readable files."""
        out: list[Path] = []
        for raw in raw_paths[:MAX_FILES]:
            try:
                path = Path(raw)
                # Directories are skipped rather than walked: recursing into
                # a dropped folder could silently process thousands of files.
                if path.is_file() and path.stat().st_size > 0:
                    out.append(path)
            except OSError:
                continue
        return out

    # ---- states ------------------------------------------------------

    def _publish_offer(self, operations: list[media.Operation]) -> None:
        count = len(self._paths)
        if count == 1:
            title = self._paths[0].name
            detail = media.human_bytes(self._paths[0].stat().st_size)
        else:
            total = sum(p.stat().st_size for p in self._paths)
            title = f"{count} files"
            detail = f"{media.human_bytes(total)} total"

        actions = tuple(
            Action(
                media.LABELS[operation],
                lambda op=operation: self._start(op),
            )
            for operation in operations
        )
        self._handle.publish_content(
            IslandContent(
                key="drop.offer",
                priority=Priority.ACTIVITY,
                title=title,
                detail=detail,
                glyph="download",
                urgency=Urgency.INFO,
                actions=actions,
                auto_expand=True,
                collapsed=f"{count} dropped",
                expires_at=ProviderHandle.ttl(OFFER_TTL),
            )
        )

    def _start(self, operation: media.Operation) -> None:
        if self._busy or not self._paths:
            return
        self._busy = True
        paths = list(self._paths)
        self._publish_working(operation, 0, len(paths), paths[0].name)

        task = _BatchTask(operation, paths)
        self._running.add(task)
        task.signals.progress.connect(
            lambda done, total, name, op=operation: self._publish_working(
                op, done, total, name
            )
        )
        task.signals.finished.connect(
            lambda results, op=operation, t=task: self._on_batch_done(op, results, t)
        )
        QThreadPool.globalInstance().start(task)

    def _on_batch_done(
        self,
        operation: media.Operation,
        results: list[media.Result],
        task: _BatchTask,
    ) -> None:
        self._running.discard(task)
        self._publish_results(operation, results)

    def _publish_working(
        self,
        operation: media.Operation,
        done: int,
        total: int,
        name: str,
    ) -> None:
        verb = media.LABELS[operation].lower()
        if total > 1:
            detail = f"{done} of {total} · {name}" if name else f"{total} files"
            progress = done / total
        else:
            # A single file gives no meaningful fraction: OCR is one opaque
            # step, so an indeterminate bar is the honest display.
            detail = name or self._paths[0].name
            progress = -1.0
        self._handle.publish_content(
            IslandContent(
                key="drop.working",
                priority=Priority.ACTIVITY,
                title=f"{verb}…".capitalize(),
                detail=detail,
                glyph=media.GLYPHS[operation],
                progress=progress,
                urgency=Urgency.INFO,
                auto_expand=True,
                collapsed=f"{verb}…",
                expires_at=ProviderHandle.ttl(300),
            )
        )

    def _publish_results(
        self, operation: media.Operation, results: list[media.Result]
    ) -> None:
        self._busy = False
        self._handle.retract("working")
        self._handle.retract("offer")

        succeeded = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]

        if operation is media.Operation.EXTRACT_TEXT:
            self._finish_text(succeeded, failed)
            return
        self._finish_files(operation, succeeded, failed)

    # ---- completion --------------------------------------------------

    def _finish_text(
        self, succeeded: list[media.Result], failed: list[media.Result]
    ) -> None:
        text = "\n\n".join(r.text for r in succeeded if r.text)
        if not text:
            reason = failed[0].message if failed else "no text found"
            self._publish_error(reason)
            return

        self._last_text = text
        copied = self._copy_to_clipboard(text)
        words = len(text.split())
        if len(succeeded) > 1:
            detail = f"from {len(succeeded)} files"
        else:
            detail = succeeded[0].source.name
        if failed:
            detail += f" · {len(failed)} failed"

        self._handle.publish_content(
            IslandContent(
                key="drop.result",
                priority=Priority.ACTIVITY,
                title=(
                    f"{words} words copied" if copied else f"{words} words extracted"
                ),
                detail=detail,
                glyph="text",
                urgency=Urgency.SUCCESS,
                actions=(Action("Copy again", self._recopy),),
                auto_expand=True,
                collapsed=f"{words} words copied",
                expires_at=ProviderHandle.ttl(RESULT_TTL),
            )
        )
        self.completed.emit(f"{words} words copied to the clipboard")

    def _finish_files(
        self,
        operation: media.Operation,
        succeeded: list[media.Result],
        failed: list[media.Result],
    ) -> None:
        if not succeeded:
            reason = failed[0].message if failed else "nothing to do"
            self._publish_error(reason)
            return

        saved = sum(r.saved_bytes for r in succeeded)
        before = sum(r.bytes_before for r in succeeded)
        self._last_output = succeeded[0].output

        if len(succeeded) == 1:
            title = succeeded[0].output.name if succeeded[0].output else "done"
            detail = succeeded[0].message
        else:
            percent = (100.0 * saved / before) if before else 0.0
            title = f"{len(succeeded)} files written"
            detail = f"{media.human_bytes(saved)} saved · {percent:.0f}% smaller"
        if failed:
            detail += f" · {len(failed)} skipped"

        self._handle.publish_content(
            IslandContent(
                key="drop.result",
                priority=Priority.ACTIVITY,
                title=title,
                detail=detail,
                glyph=media.GLYPHS[operation],
                urgency=Urgency.SUCCESS,
                actions=(Action("Show in folder", self._reveal),),
                auto_expand=True,
                collapsed=f"saved {media.human_bytes(saved)}",
                expires_at=ProviderHandle.ttl(RESULT_TTL),
            )
        )
        self.completed.emit(f"{title} · {detail}")

    def _publish_error(self, message: str) -> None:
        self._busy = False
        self._handle.retract("offer")
        self._handle.retract("working")
        self._handle.publish_content(
            IslandContent(
                key="drop.result",
                priority=Priority.ACTIVITY,
                title="Nothing done",
                detail=message,
                glyph="warning",
                urgency=Urgency.WARNING,
                collapsed="drop failed",
                expires_at=ProviderHandle.ttl(8),
            )
        )

    # ---- side effects ------------------------------------------------

    @staticmethod
    def _copy_to_clipboard(text: str) -> bool:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        return True

    def _recopy(self) -> None:
        if self._last_text:
            self._copy_to_clipboard(self._last_text)

    def _reveal(self) -> None:
        """Open Explorer with the produced file selected."""
        target = self._last_output
        if target is None or not target.exists():
            return
        try:
            subprocess.Popen(
                ["explorer", "/select,", str(target)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            # Fall back to just opening the folder. Failing to reveal a file
            # is not worth surfacing an error for.
            with contextlib.suppress(OSError):
                os.startfile(str(target.parent))
