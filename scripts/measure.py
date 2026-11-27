"""Measure the island against its stated budgets.

docs/architecture.md commits to numbers for idle memory, idle CPU and cold
start. Numbers in a document that nobody measures drift into fiction, so
this script measures the real process and exits non-zero when a budget is
exceeded.

Run::

    .venv/Scripts/python.exe scripts/measure.py [sample_seconds]

Note the measurement is of a source checkout, which carries the Python
interpreter and uncompiled bytecode. A Nuitka build should come in lower.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

START = time.perf_counter()

import psutil
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from main import Application

# Budgets from docs/architecture.md.
BUDGET_RSS_MB = 80.0
BUDGET_CPU_PERCENT = 0.5
BUDGET_COLD_START_S = 1.5


def main() -> int:
    sample_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    controller = Application(app)
    controller.start()

    process = psutil.Process(os.getpid())
    results: dict[str, float] = {}

    def on_visible() -> None:
        results["cold_start"] = time.perf_counter() - START
        # Baseline the CPU counter, then let the app sit idle.
        process.cpu_percent(interval=None)
        results["rss_at_start"] = process.memory_info().rss / (1024 * 1024)
        QTimer.singleShot(int(sample_seconds * 1000), finish)

    def finish() -> None:
        # cpu_percent here is a share of one core, normalised by psutil over
        # the interval since the baseline call above.
        results["cpu"] = process.cpu_percent(interval=None) / psutil.cpu_count()
        results["rss"] = process.memory_info().rss / (1024 * 1024)
        results["threads"] = process.num_threads()
        results["handles"] = getattr(process, "num_handles", lambda: 0)()
        controller.shutdown()

    # Wait until the window is actually mapped before calling it started.
    def wait_for_window() -> None:
        if controller.window.isVisible():
            on_visible()
        else:
            QTimer.singleShot(10, wait_for_window)

    QTimer.singleShot(0, wait_for_window)
    app.exec()

    if "cpu" not in results:
        print("FAILED: the island never became visible")
        return 1

    # The optional feature packs are the whole reason the idle figure is
    # achievable. If one of them ever gets imported at startup the memory
    # budget quietly doubles, so check directly rather than inferring it
    # from the number.
    eager = [
        name
        for name in ("rapidocr_onnxruntime", "onnxruntime", "cv2", "PIL.Image")
        if name in sys.modules
    ]

    rows = [
        ("cold start to visible pill", results["cold_start"], BUDGET_COLD_START_S, "s"),
        ("idle resident memory", results["rss"], BUDGET_RSS_MB, "MB"),
        ("idle CPU", results["cpu"], BUDGET_CPU_PERCENT, "%"),
    ]

    print(f"\nmeasured over {sample_seconds:.0f}s idle\n")
    print(f"{'metric':<32}{'measured':>12}{'budget':>12}   verdict")
    print("-" * 70)
    failures = 0
    for label, value, budget, unit in rows:
        ok = value <= budget
        failures += 0 if ok else 1
        print(
            f"{label:<32}{value:>10.2f} {unit:<2}{budget:>10.2f} {unit:<2}"
            f"   {'ok' if ok else 'OVER BUDGET'}"
        )
    print("-" * 70)
    print(f"{'threads':<32}{results['threads']:>10.0f}")
    print(f"{'os handles':<32}{results['handles']:>10.0f}")
    print(f"{'rss at startup':<32}{results['rss_at_start']:>10.2f} MB")

    print("\nlazy feature packs")
    if eager:
        failures += 1
        for name in eager:
            print(f"  [FAIL] {name} was imported at startup")
    else:
        print("  [ok  ] OCR, ONNX and OpenCV stay unimported until used")

    if failures:
        print(f"\n{failures} budget(s) exceeded.")
        return 1
    print("\nAll budgets met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
