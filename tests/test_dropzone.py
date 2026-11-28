"""Drop-to-Action flow, and the island's collapse behaviour around it.

Both of the tests about cancelling and collapsing exist because of
feedback from using the app. The collapse one in particular was a bug the
suite could never have caught, because nothing in it ever let a piece of
auto-expanding content expire.
"""

from __future__ import annotations

import pytest

from core.config import DEFAULTS, Settings, _deep_merge
from core.island_window import IslandWindow
from core.model import IslandContent, Priority
from core.theme import load_theme
from workers.dropzone import DropActionProvider


@pytest.fixture
def settings(tmp_path):
    return Settings(_deep_merge(DEFAULTS, {}), tmp_path / "settings.json")


@pytest.fixture
def provider(arbiter, qapp):
    return DropActionProvider(arbiter)


@pytest.fixture
def window(qapp):
    instance = IslandWindow(load_theme(None))
    yield instance
    instance.close()
    instance.deleteLater()


def make_png(path):
    pytest.importorskip("PIL", reason="Pillow is an optional feature pack")
    from PIL import Image

    Image.new("RGB", (80, 60), (200, 40, 40)).save(path)
    return path


# ---- the offer -----------------------------------------------------------


def test_dropping_a_file_offers_actions(provider, arbiter, tmp_path):
    make_png(tmp_path / "shot.png")
    provider.handle_drop([str(tmp_path / "shot.png")])
    content = arbiter.current()
    assert content.key == "drop.offer"
    labels = [a.label for a in content.actions]
    assert "Extract text" in labels
    assert "Compress" in labels


def test_the_offer_can_be_cancelled(provider, arbiter, tmp_path):
    # Without this the only way out of a mis-drop was to wait for the
    # offer to time out.
    make_png(tmp_path / "shot.png")
    provider.handle_drop([str(tmp_path / "shot.png")])
    cancel = next(
        a for a in arbiter.current().actions if a.label == "Cancel"
    )
    cancel.callback()
    assert "drop.offer" not in [c.key for c in arbiter.candidates()]


def test_cancel_is_the_rightmost_action(provider, arbiter, tmp_path):
    make_png(tmp_path / "shot.png")
    provider.handle_drop([str(tmp_path / "shot.png")])
    assert arbiter.current().actions[-1].label == "Cancel"


def test_cancelling_forgets_the_dropped_files(provider, arbiter, tmp_path):
    make_png(tmp_path / "shot.png")
    provider.handle_drop([str(tmp_path / "shot.png")])
    provider.cancel()
    # A later action must not operate on the files from a cancelled drop.
    assert provider._paths == []


def test_an_offer_expires_on_its_own(provider, arbiter, tmp_path):
    make_png(tmp_path / "shot.png")
    provider.handle_drop([str(tmp_path / "shot.png")])
    assert arbiter.current().expires_at is not None


def test_dropping_nothing_usable_is_reported(provider, arbiter, tmp_path):
    missing = tmp_path / "gone.png"
    provider.handle_drop([str(missing)])
    content = arbiter.current()
    assert content.key == "drop.result"
    assert content.urgency.name == "WARNING"


def test_a_directory_is_not_walked(provider, arbiter, tmp_path):
    folder = tmp_path / "stuff"
    folder.mkdir()
    make_png(folder / "a.png")
    provider.handle_drop([str(folder)])
    # Recursing into a dropped folder could process thousands of files.
    assert arbiter.current().key == "drop.result"


# ---- the island collapsing again ----------------------------------------


def expand_content(key: str) -> IslandContent:
    return IslandContent(
        key=key, priority=Priority.ACTIVITY, title="drop", auto_expand=True
    )


def plain_content(key: str = "ambient.clock") -> IslandContent:
    return IslandContent(key=key, priority=Priority.AMBIENT, title="09:41")


def test_auto_expanding_content_opens_the_island(window):
    window.set_content(plain_content())
    assert window.is_expanded() is False
    window.set_content(expand_content("drop.offer"))
    assert window.is_expanded() is True


def test_the_island_closes_when_that_content_goes_away(window):
    """The bug: it used to stay open around whatever replaced it.

    Nothing collapsed the island once the thing that had opened it
    expired, so the clock sat in a full-size card until the user happened
    to hover the island and move away again.
    """
    window.set_content(expand_content("drop.offer"))
    assert window.is_expanded() is True
    window.set_content(plain_content())
    assert window.is_expanded() is False


def test_hovering_keeps_it_open(window):
    window.set_content(expand_content("drop.offer"))
    window._hovered = True
    window.set_content(plain_content())
    # Collapsing under the cursor while someone is reading would be worse.
    assert window.is_expanded() is True


def test_a_pinned_island_stays_open(window):
    window.set_content(expand_content("drop.offer"))
    window.set_pinned(True)
    window.set_content(plain_content())
    # The control panel hangs off the island's lower edge.
    assert window.is_expanded() is True


def test_one_auto_expanding_item_replacing_another_stays_open(window):
    window.set_content(expand_content("drop.offer"))
    window.set_content(expand_content("drop.working"))
    assert window.is_expanded() is True
