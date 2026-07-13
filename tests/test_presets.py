"""
test_presets.py — keep the preset picker and the renderer palettes in lockstep.

PRESET_INFO is the single source of truth: the renderer derives its color palettes
from it and the UI derives its buttons/popovers from it. These tests pin the
invariants so the two can't drift — every theme has honest UI copy AND a complete,
colors-only palette, and the picker exposes exactly those themes (plus "None").
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLY_DB_PATH", "/tmp/test_presets.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import app        # noqa: E402
import renderer   # noqa: E402

_PALETTE_KEYS = {"bg", "atmosphere", "accent", "grain"}


def test_every_preset_has_copy_and_a_complete_palette():
    for p in renderer.PRESET_INFO:
        assert p["name"] and p["label"], p
        assert p.get("description", "").strip(), f"{p['name']} needs a description"
        assert _PALETTE_KEYS <= set(p), f"{p['name']} missing palette keys"


def test_PRESETS_is_derived_and_colors_only():
    # Same set of names as PRESET_INFO...
    assert set(renderer.PRESETS) == {p["name"] for p in renderer.PRESET_INFO}
    # ...and each palette carries ONLY color keys, so label/description/name never
    # leak into the palette dict injected into scene code.
    for name, cols in renderer.PRESETS.items():
        assert set(cols) == _PALETTE_KEYS, (name, set(cols))


def test_index_assets_are_cache_busted():
    # A stale cached app.css once masked the new popover styles (descriptions
    # dumped inline). The CSS/JS links must carry a ?v= mtime so a deploy always
    # invalidates the browser cache.
    import re
    html = app.app.test_client().get("/").get_data(as_text=True)
    assert re.search(r"app\.css\?v=\d+", html), "app.css link is not cache-busted"
    assert re.search(r"app\.js\?v=\d+", html), "app.js link is not cache-busted"


def test_picker_leads_with_none_then_all_presets():
    ui = app._preset_ui()
    assert ui[0]["name"] == "" and ui[0]["label"] == "None"
    assert [u["name"] for u in ui[1:]] == [p["name"] for p in renderer.PRESET_INFO]
    for u in ui:
        assert u["description"].strip() and u["swatch"].startswith("#")
