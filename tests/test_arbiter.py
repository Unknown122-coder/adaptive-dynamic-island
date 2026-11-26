"""Arbitration rules.

These are the rules the whole UI depends on, so they are tested directly
rather than through the window: a privacy warning must never be buried, an
activity must outrank a clock, and a provider that goes silent must not be
able to leave content stuck on screen.
"""

from __future__ import annotations

import time

import pytest

from core.arbiter import ProviderHandle
from core.model import IslandContent, Priority


def content(key: str, priority: Priority, title: str = "x") -> IslandContent:
    return IslandContent(key=key, priority=priority, title=title)


def test_empty_arbiter_has_no_winner(arbiter):
    assert arbiter.current() is None
    assert arbiter.candidates() == []


def test_higher_priority_wins(arbiter):
    arbiter.publish(content("ambient.clock", Priority.AMBIENT))
    arbiter.publish(content("activity.build", Priority.ACTIVITY))
    assert arbiter.current().key == "activity.build"


def test_privacy_outranks_everything(arbiter):
    for key, priority in [
        ("ambient.clock", Priority.AMBIENT),
        ("context.dev", Priority.CONTEXT),
        ("activity.build", Priority.ACTIVITY),
        ("alert.cpu", Priority.ALERT),
        ("privacy.faces", Priority.PRIVACY),
    ]:
        arbiter.publish(content(key, priority))
    assert arbiter.current().key == "privacy.faces"


def test_ties_broken_by_most_recent(arbiter):
    arbiter.publish(content("activity.a", Priority.ACTIVITY))
    time.sleep(0.01)
    arbiter.publish(content("activity.b", Priority.ACTIVITY))
    assert arbiter.current().key == "activity.b"


def test_retracting_the_winner_falls_back(arbiter):
    arbiter.publish(content("ambient.clock", Priority.AMBIENT))
    arbiter.publish(content("activity.build", Priority.ACTIVITY))
    arbiter.retract("activity.build")
    assert arbiter.current().key == "ambient.clock"


def test_update_in_place_keeps_identity(arbiter):
    arbiter.publish(
        IslandContent(
            key="activity.build",
            priority=Priority.ACTIVITY,
            title="build",
            progress=0.1,
        )
    )
    arbiter.publish(
        IslandContent(
            key="activity.build",
            priority=Priority.ACTIVITY,
            title="build",
            progress=0.9,
        )
    )
    assert len(arbiter.candidates()) == 1
    assert arbiter.current().progress == pytest.approx(0.9)


def test_expired_content_is_swept(arbiter):
    arbiter.publish(
        IslandContent(
            key="activity.dead",
            priority=Priority.ACTIVITY,
            title="abandoned",
            # Already in the past: a producer that died mid-run.
            expires_at=time.time() - 1,
        )
    )
    arbiter._sweep()
    assert arbiter.current() is None


def test_candidates_are_ordered_by_priority(arbiter):
    arbiter.publish(content("ambient.clock", Priority.AMBIENT))
    arbiter.publish(content("alert.cpu", Priority.ALERT))
    arbiter.publish(content("context.dev", Priority.CONTEXT))
    order = [c.priority for c in arbiter.candidates()]
    assert order == sorted(order, reverse=True)


def test_provider_handle_namespaces_keys(arbiter):
    handle = ProviderHandle(arbiter, "activity")
    handle.publish("build", Priority.ACTIVITY, "docker build")
    assert arbiter.current().key == "activity.build"


def test_provider_handle_clear_only_clears_its_own(arbiter):
    activities = ProviderHandle(arbiter, "activity")
    ambient = ProviderHandle(arbiter, "ambient")
    activities.publish("build", Priority.ACTIVITY, "docker build")
    ambient.publish("clock", Priority.AMBIENT, "09:41")

    activities.clear()

    keys = [c.key for c in arbiter.candidates()]
    assert keys == ["ambient.clock"]


def test_content_changed_is_emitted(arbiter, qapp):
    seen = []
    arbiter.content_changed.connect(seen.append)
    arbiter.publish(content("ambient.clock", Priority.AMBIENT))
    assert seen and seen[-1].key == "ambient.clock"
