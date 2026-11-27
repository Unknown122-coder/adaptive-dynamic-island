"""Theme and settings behaviour.

The promise made to users is that a theme file can be partial and a broken
one will not stop the island from starting. These tests hold that promise
in place.
"""

from __future__ import annotations

import json

from core import config
from core import theme as theme_module

# ---- themes --------------------------------------------------------------


def test_defaults_load_without_a_file():
    loaded = theme_module.load_theme(None)
    assert loaded.name == "Midnight OLED"
    assert loaded.metric("collapsed_height") > 0


def test_partial_theme_merges_over_defaults(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(
        json.dumps({"name": "Custom", "colors": {"text": "#FF0000"}}),
        encoding="utf-8",
    )
    loaded = theme_module.load_theme(path)
    assert loaded.name == "Custom"
    assert loaded.color("text").red() == 255
    # Untouched keys still come from the defaults.
    assert loaded.metric("expanded_width") == (
        theme_module.DEFAULT_THEME["metrics"]["expanded_width"]
    )


def test_malformed_theme_falls_back(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json", encoding="utf-8")
    loaded = theme_module.load_theme(path)
    assert loaded.name == "Midnight OLED"
    assert loaded.source is None
    assert "falling back" in capsys.readouterr().out


def test_theme_root_must_be_an_object(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1,2,3]", encoding="utf-8")
    assert theme_module.load_theme(path).name == "Midnight OLED"


def test_rgba_hex_uses_web_channel_order():
    # Qt's own parser reads #AARRGGBB, which no design tool emits.
    color = theme_module.Theme._parse_color("#11223344")
    assert (color.red(), color.green(), color.blue(), color.alpha()) == (
        0x11,
        0x22,
        0x33,
        0x44,
    )


def test_unknown_colour_does_not_raise():
    color = theme_module.Theme._parse_color("not-a-colour")
    assert color.alpha() > 0


def test_named_motion_curve_is_built(qapp):
    loaded = theme_module.load_theme(None)
    assert loaded.morph_curve() is not None


def test_bezier_motion_curve_is_accepted(tmp_path, qapp):
    path = tmp_path / "bezier.json"
    path.write_text(
        json.dumps({"motion": {"morph_curve": [0.2, 0.0, 0.1, 1.0]}}),
        encoding="utf-8",
    )
    assert theme_module.load_theme(path).morph_curve() is not None


def test_accent_falls_back_for_unknown_urgency():
    loaded = theme_module.load_theme(None)
    assert loaded.accent_for("NONSENSE") == loaded.color("accent_neutral")


# ---- settings ------------------------------------------------------------


def make_settings(tmp_path, data=None):
    return config.Settings(
        config._deep_merge(config.DEFAULTS, data or {}),
        tmp_path / "settings.json",
    )


def test_dotted_get_reads_nested_values(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.get("display.preference") == "internal"


def test_dotted_get_returns_default_for_missing(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.get("nothing.here", "fallback") == "fallback"


def test_dotted_set_creates_intermediate_levels(tmp_path):
    settings = make_settings(tmp_path)
    settings.set("a.b.c", 7)
    assert settings.get("a.b.c") == 7


def test_toggle_flips_and_returns(tmp_path):
    settings = make_settings(tmp_path)
    settings.set("providers.privacy", False)
    assert settings.toggle("providers.privacy") is True
    assert settings.get("providers.privacy") is True


def test_lowered_list_normalises_case(tmp_path):
    settings = make_settings(
        tmp_path, {"context": {"browser_processes": ["Chrome.EXE"]}}
    )
    assert "chrome.exe" in settings.lowered_list("context.browser_processes")


def test_save_then_reload_round_trips(tmp_path):
    settings = make_settings(tmp_path)
    settings.set("theme", "high_contrast")
    assert settings.save()
    written = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert written["theme"] == "high_contrast"


def test_save_leaves_no_temporary_files(tmp_path):
    settings = make_settings(tmp_path)
    settings.save()
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".settings-")]
    assert leftovers == []


def test_zero_is_kept_not_replaced_by_the_default(tmp_path):
    # The shorthand `get(key, default) or default` silently turns a user's
    # explicit 0 back into the default, so a setting used to switch a
    # feature off does nothing. This is the guard against that.
    settings = make_settings(tmp_path)
    settings.set("privacy.walk_away_seconds", 0)
    assert settings.number("privacy.walk_away_seconds", 60) == 0.0
    assert settings.integer("privacy.walk_away_seconds", 60) == 0


def test_number_falls_back_when_missing(tmp_path):
    settings = make_settings(tmp_path)
    assert settings.number("nothing.here", 2.5) == 2.5


def test_number_falls_back_on_junk(tmp_path):
    settings = make_settings(tmp_path)
    settings.set("privacy.frames_per_second", "fast")
    assert settings.number("privacy.frames_per_second", 1.0) == 1.0


def test_number_rejects_booleans(tmp_path):
    # True would otherwise quietly become 1.0 and look like a valid rate.
    settings = make_settings(tmp_path)
    settings.set("privacy.frames_per_second", True)
    assert settings.number("privacy.frames_per_second", 4.0) == 4.0


def test_number_accepts_a_numeric_string(tmp_path):
    settings = make_settings(tmp_path)
    settings.set("privacy.walk_away_seconds", "90")
    assert settings.number("privacy.walk_away_seconds", 60) == 90.0


def test_privacy_is_off_by_default():
    # Anything that opens the camera must be opt-in.
    assert config.DEFAULTS["privacy"]["enabled"] is False
    assert config.DEFAULTS["providers"]["privacy"] is False
