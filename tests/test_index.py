"""
test_index.py — invariants of the index page that aren't about a specific feature.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLY_DB_PATH", "/tmp/test_index.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import app  # noqa: E402


def test_index_assets_are_cache_busted():
    # A stale cached app.css once masked a CSS change. The CSS/JS links must carry
    # a ?v= mtime so a deploy always invalidates the browser cache.
    html = app.app.test_client().get("/").get_data(as_text=True)
    assert re.search(r"app\.css\?v=\d+", html), "app.css link is not cache-busted"
    assert re.search(r"app\.js\?v=\d+", html), "app.js link is not cache-busted"
