"""
test_prompt_capabilities.py — keep the JSON system prompt and the interpreter in
lockstep.

In SCENE_FORMAT=json the model supplies all the artistic detail from a general
user prompt, so it must be *aware* of every shape it can draw. If a primitive
exists in scene_json but isn't advertised in SYSTEM_PROMPT_JSON, the model will
never use it; if the prompt advertises an op the interpreter doesn't implement,
that scene 400s. This test fails on either kind of drift.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing app constructs the Flask app + inits a DB; keep it off the real one.
os.environ.setdefault("POLY_DB_PATH", "/tmp/test_prompt_caps.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import app           # noqa: E402
import scene_json    # noqa: E402

_ADVERTISED_OPS = set(re.findall(r'"op"\s*:\s*"(\w+)"', app.SYSTEM_PROMPT_JSON))
_ADVERTISED_BGS = set(re.findall(r'"type"\s*:\s*"(\w+)"', app.SYSTEM_PROMPT_JSON))
_IMPLEMENTED_BGS = {"solid", "gradient", "radial"}


def test_every_implemented_op_is_advertised():
    missing = set(scene_json.ALLOWED_OPS) - _ADVERTISED_OPS
    assert not missing, f"ops the model is never told about: {sorted(missing)}"


def test_no_advertised_op_is_unimplemented():
    phantom = _ADVERTISED_OPS - set(scene_json.ALLOWED_OPS)
    assert not phantom, f"prompt advertises ops the interpreter can't draw: {sorted(phantom)}"


def test_background_types_match():
    assert _IMPLEMENTED_BGS <= _ADVERTISED_BGS, \
        f"background types not advertised: {sorted(_IMPLEMENTED_BGS - _ADVERTISED_BGS)}"


def test_limits_are_stated():
    # The hard caps must be visible to the model so it stays within budget.
    assert str(scene_json.MAX_LAYERS) in app.SYSTEM_PROMPT_JSON
    assert str(scene_json.MAX_OPS) in app.SYSTEM_PROMPT_JSON
    assert str(scene_json.MAX_DRAWS) in app.SYSTEM_PROMPT_JSON


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
