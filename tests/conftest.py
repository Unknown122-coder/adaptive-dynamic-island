"""Shared fixtures.

Qt objects need a live application instance even when nothing is shown, so
one is created per test session on the offscreen platform plugin. That lets
the whole suite run under CI with no display attached.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def arbiter(qapp):
    from core.arbiter import ContentArbiter

    instance = ContentArbiter()
    yield instance
    instance.deleteLater()
