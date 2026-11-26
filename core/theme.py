"""JSON theme loading with deep-merge over built-in defaults.

A theme file may specify as little as one colour. Everything unspecified
falls back to :data:`DEFAULT_THEME`, so a user's hand-written theme can never
leave the island half-styled or crash it on a missing key -- a failure mode
that makes "customisable" features unusable in practice.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEasingCurve, QPointF
from PySide6.QtGui import QColor

DEFAULT_THEME: dict[str, Any] = {
    "name": "Midnight OLED",
    "metrics": {
        # The collapsed pill is deliberately narrow: it must read as part of
        # the bezel, not as a window sitting on the desktop.
        "collapsed_width": 168,
        "collapsed_height": 30,
        "expanded_width": 404,
        "expanded_height": 92,
        "radius_collapsed": 15,
        "radius_expanded": 22,
        # Distance from the top of the screen. A couple of pixels of black
        # above the pill is what sells the "part of the bezel" illusion.
        "top_inset": 1,
        "padding_h": 14,
        "padding_v": 8,
        "glyph_size": 15,
        "title_size": 11,
        "detail_size": 10,
        "progress_height": 3,
        "border_width": 1,
    },
    "motion": {
        "morph_ms": 340,
        "morph_curve": "spring",
        "spring_overshoot": 1.15,
        "fade_ms": 140,
        # Hover intent delays. Without them the island twitches every time
        # the cursor crosses the top of the screen on its way to a tab.
        "hover_open_delay_ms": 110,
        "hover_close_delay_ms": 260,
    },
    "colors": {
        "surface": "#0B0B0DF2",
        "surface_hover": "#15151AF7",
        "border": "#FFFFFF1F",
        "text": "#F2F3F5",
        "text_dim": "#9AA0A8",
        "glyph": "#E6E8EB",
        "progress_track": "#FFFFFF1A",
        "progress_fill": "#5B8CFF",
        "accent_neutral": "#5B8CFF",
        "accent_info": "#4CA9FF",
        "accent_success": "#3FCF8E",
        "accent_warning": "#F5A524",
        "accent_critical": "#F5455C",
        "shadow": "#000000A0",
    },
    "effects": {
        "shadow_blur": 16,
        "shadow_offset_y": 6,
        # A hairline accent along the top edge, matching the urgency colour.
        "urgency_edge": True,
    },
}

_NAMED_CURVES = {
    "linear": QEasingCurve.Type.Linear,
    "ease_out": QEasingCurve.Type.OutCubic,
    "ease_in_out": QEasingCurve.Type.InOutCubic,
    "ease_out_quint": QEasingCurve.Type.OutQuint,
    "elastic": QEasingCurve.Type.OutElastic,
    "back": QEasingCurve.Type.OutBack,
    "spring": QEasingCurve.Type.OutBack,
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Theme:
    """Resolved theme. Access is by section to keep call sites readable."""

    data: dict[str, Any]
    source: Path | None = None

    @property
    def name(self) -> str:
        return str(self.data.get("name", "Unnamed"))

    def metric(self, key: str) -> int:
        return int(self.data["metrics"][key])

    def motion(self, key: str) -> Any:
        return self.data["motion"][key]

    def color(self, key: str) -> QColor:
        raw = str(self.data["colors"][key])
        return self._parse_color(raw)

    def effect(self, key: str) -> Any:
        return self.data["effects"][key]

    def accent_for(self, urgency_name: str) -> QColor:
        key = f"accent_{urgency_name.lower()}"
        if key in self.data["colors"]:
            return self.color(key)
        return self.color("accent_neutral")

    def morph_curve(self) -> QEasingCurve:
        """Build the easing curve named by the theme.

        Supports the named presets plus ``"cubic-bezier"`` with four control
        values, so a user can hand-tune the motion without touching Python.
        """
        spec = self.motion("morph_curve")
        if isinstance(spec, list) and len(spec) == 4:
            curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
            x1, y1, x2, y2 = (float(v) for v in spec)
            curve.addCubicBezierSegment(
                QPointF(x1, y1), QPointF(x2, y2), QPointF(1.0, 1.0)
            )
            return curve
        name = str(spec).lower()
        curve = QEasingCurve(_NAMED_CURVES.get(name, QEasingCurve.Type.OutBack))
        if name in ("spring", "back"):
            # OutBack's default overshoot (1.70158) is cartoonish at this
            # size. The theme exposes it so the bounce can be tuned.
            curve.setOvershoot(float(self.motion("spring_overshoot")))
        return curve

    @staticmethod
    def _parse_color(raw: str) -> QColor:
        """Accept ``#RGB``, ``#RRGGBB``, ``#RRGGBBAA`` or a Qt colour name.

        Qt's own parser expects ``#AARRGGBB``, which no designer writes and
        no CSS tool emits, so the common web ordering is converted here.
        """
        s = raw.strip()
        if s.startswith("#") and len(s) == 9:
            r, g, b, a = (int(s[i : i + 2], 16) for i in (1, 3, 5, 7))
            return QColor(r, g, b, a)
        c = QColor(s)
        return c if c.isValid() else QColor(0, 0, 0, 230)


def load_theme(path: str | Path | None) -> Theme:
    """Load a theme file, merging it over the defaults.

    A missing, unreadable or malformed file is not fatal: the island falls
    back to the built-in theme and the caller can surface the problem.
    """
    if path is None:
        return Theme(copy.deepcopy(DEFAULT_THEME), None)
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("theme root must be a JSON object")
        return Theme(_deep_merge(DEFAULT_THEME, raw), p)
    except (OSError, ValueError) as exc:  # includes JSONDecodeError
        print(f"[theme] falling back to defaults, {p.name}: {exc}")
        return Theme(copy.deepcopy(DEFAULT_THEME), None)
