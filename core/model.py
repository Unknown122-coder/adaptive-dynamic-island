"""Content model for the island.

Every subsystem -- the context engine, the live-activity bus, the system
sentinel, the privacy guard, user plugins -- produces the *same* data type:
an :class:`IslandContent`. The window itself knows nothing about Git, webcams
or CPU load; it only renders whichever content currently wins arbitration.

That indirection is the reason the island can grow features without the
window growing complexity, and it is what separates this from the common
"one widget per feature, hard-coded" clone.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

# Publication order. time.monotonic() on Windows advances in ~15 ms steps,
# so two providers publishing in the same tick would compare equal and the
# arbiter's tie-break would depend on dict ordering. A counter makes the
# ordering total and explicit.
_SEQUENCE = itertools.count(1)


class Priority(IntEnum):
    """Arbitration weight. Higher wins.

    The ordering encodes a product decision: something that protects the
    user (privacy) must never be buried by something merely informative
    (a clock). Ties are broken by most-recent-update.
    """

    AMBIENT = 10      # clock, media, idle chrome
    CONTEXT = 30      # app-profile chrome (dev / meeting / browser)
    PLUGIN = 40       # third-party widgets
    ACTIVITY = 60     # live activities: builds, tests, downloads, transfers
    ALERT = 80        # degraded system / network state
    PRIVACY = 100     # shoulder-surfing or camera-in-use warnings


class Urgency(IntEnum):
    """Visual temperature, independent of priority."""

    NEUTRAL = 0
    INFO = 1
    SUCCESS = 2
    WARNING = 3
    CRITICAL = 4


@dataclass(frozen=True)
class Action:
    """A button the island can offer in its expanded state."""

    label: str
    callback: Callable[[], None]
    # A destructive action gets confirm-on-second-click rather than firing
    # immediately, because the island sits under the cursor's travel path.
    destructive: bool = False


@dataclass
class IslandContent:
    """One renderable item.

    ``key`` is stable per logical item so a provider can update in place
    (a build going 10% -> 90%) without the island treating it as new
    content and replaying the entrance animation.
    """

    key: str
    priority: Priority
    title: str
    detail: str | None = None
    glyph: str | None = None
    # 0.0-1.0 for a determinate bar, -1.0 for indeterminate, None for no bar.
    progress: float | None = None
    urgency: Urgency = Urgency.NEUTRAL
    actions: tuple[Action, ...] = field(default_factory=tuple)
    # Text for the collapsed pill. Falls back to ``title`` when unset.
    collapsed: str | None = None
    # Wall-clock deadline after which the arbiter drops this content.
    expires_at: float | None = None
    # Content that should pop the island open by itself rather than waiting
    # for a hover. Reserve this for ALERT and PRIVACY.
    auto_expand: bool = False
    updated_at: float = field(default_factory=time.monotonic)
    # Strictly increasing publication order, used for tie-breaking.
    seq: int = field(default_factory=lambda: next(_SEQUENCE))

    def touch(self) -> None:
        self.updated_at = time.monotonic()
        self.seq = next(_SEQUENCE)

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at

    @property
    def collapsed_text(self) -> str:
        return self.collapsed if self.collapsed is not None else self.title

    @property
    def indeterminate(self) -> bool:
        return self.progress is not None and self.progress < 0

    def replaces(self, other: IslandContent | None) -> bool:
        """True when swapping to ``self`` is a *new* item, not an update."""
        return other is None or other.key != self.key
