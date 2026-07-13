"""
test_generation_truncation.py — a truncated model response must be handled as
truncation, not as a syntax/schema error.

The house-style scene code (and a busy JSON scene) can run long. When the model
hits the output-token cap, its reply is cut off mid-statement: the Python tail
leaves an unclosed '(' and the JSON tail is an unterminated object. Before the
fix this surfaced as a bogus "syntax error", and because the fix-retry re-hit the
same cap it "persisted after retry". These tests pin the corrected behaviour:
call_claude reports truncation, and each generation path retries once for a
complete scene, then fails with an honest message instead of looping.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLY_DB_PATH", "/tmp/test_gen_truncation.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import app  # noqa: E402


def _stub_call_claude(responses):
    """Return a call_claude replacement that yields `responses` in order and
    records how many times it was invoked."""
    state = {"i": 0, "n": 0}

    def fake(prompt, preset, seed, model, system=None, max_tokens=app.MAX_OUTPUT_TOKENS):
        state["n"] += 1
        resp = responses[min(state["i"], len(responses) - 1)]
        state["i"] += 1
        return resp

    return fake, state


def test_python_truncation_reports_honestly_and_retries_once(monkeypatch):
    # Both the first response and the retry are truncated.
    fake, state = _stub_call_claude([("img = Image.new(", 10, 20, True)] * 2)
    monkeypatch.setattr(app, "call_claude", fake)

    try:
        app._generate_python_scene("a dragon", None, 42, "claude-sonnet-4-6")
        assert False, "expected SceneGenError on persistent truncation"
    except app.SceneGenError as e:
        assert "output length limit" in e.message      # not a "syntax error"
        assert "syntax" not in e.message.lower()
        assert state["n"] == 2                          # one retry, then give up


def test_python_truncation_recovers_on_retry(monkeypatch):
    complete = 'img = Image.new("RGB", (W, H))\ndraw = ImageDraw.Draw(img)'
    fake, state = _stub_call_claude([
        ("img = Image.new(", 10, 20, True),             # truncated
        (complete, 10, 40, False),                      # complete on retry
    ])
    monkeypatch.setattr(app, "call_claude", fake)

    code, tin, tout = app._generate_python_scene("a cat", None, 42, "claude-sonnet-4-6")
    assert code == complete
    assert (tin, tout) == (20, 60)                      # tokens accumulate across calls
    assert state["n"] == 2


def test_json_truncation_reports_honestly_and_retries_once(monkeypatch):
    fake, state = _stub_call_claude([('{"layers": [{"ops": [', 10, 20, True)] * 2)
    monkeypatch.setattr(app, "call_claude", fake)

    try:
        app._generate_json_scene("a dragon", None, 42, "claude-sonnet-4-6")
        assert False, "expected SceneGenError on persistent truncation"
    except app.SceneGenError as e:
        assert "output length limit" in e.message
        assert state["n"] == 2
