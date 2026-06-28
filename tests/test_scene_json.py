"""
test_scene_json.py — the declarative JSON scene path (SCENE_FORMAT=json).

The point of this path is that there is NO code execution: the model returns
data, and scene_json validates+draws it. So the tests assert (1) a valid scene
renders, (2) the schema/DoS caps reject malformed or oversized scenes, and (3)
there is no exec surface — an op that looks like an instruction is just an
unknown op, not something that runs.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scene_json                                        # noqa: E402
import renderer                                          # noqa: E402

VALID = {
    "background": {"type": "gradient", "from": [10, 10, 30], "to": [40, 20, 60]},
    "layers": [
        {"ops": [
            {"op": "ellipse", "bbox": [50, 50, 200, 200], "fill": [200, 180, 90], "outline": [255, 255, 255], "width": 3},
            {"op": "polygon", "points": [[60, 60], [180, 70], [120, 190]], "fill": "accent"},
            {"op": "line", "points": [[0, 0], [256, 256]], "fill": [255, 0, 0], "width": 2},
            {"op": "arc", "bbox": [20, 20, 120, 120], "start": 0, "end": 180, "fill": [0, 255, 0]},
            {"op": "text", "xy": [10, 10], "text": "hi", "fill": [255, 255, 255], "size": 18},
        ]},
        {"alpha": 120, "ops": [
            {"op": "grain", "count": 800, "fill": [255, 255, 255], "alpha": 40},
            {"op": "vignette", "strength": 90},
        ]},
    ],
}

REJECTED = {
    "not_an_object":   [],
    "no_layers":       {"background": {"type": "solid", "color": [0, 0, 0]}},
    "layers_not_list": {"layers": {}},
    "unknown_op":      {"layers": [{"ops": [{"op": "system", "cmd": "id"}]}]},
    "eval_like_op":    {"layers": [{"ops": [{"op": "exec", "code": "import os"}]}]},
    "too_many_layers": {"layers": [{"ops": []} for _ in range(scene_json.MAX_LAYERS + 1)]},
    "too_many_ops":    {"layers": [{"ops": [{"op": "point", "points": [[0, 0]]}] * (scene_json.MAX_OPS + 1)}]},
    "too_many_points": {"layers": [{"ops": [{"op": "polygon", "points": [[0, 0]] * (scene_json.MAX_POINTS + 1)}]}]},
    "bad_text_type":   {"layers": [{"ops": [{"op": "text", "xy": [0, 0], "text": 123}]}]},
}


def test_valid_scene_passes_validation():
    assert scene_json.validate_scene_json(VALID) is None


def test_rejected_scenes():
    for name, scene in REJECTED.items():
        assert scene_json.validate_scene_json(scene) is not None, f"{name} was not rejected"


def test_valid_scene_renders_png_and_svg():
    with tempfile.TemporaryDirectory() as d:
        res = renderer.render_json(VALID, filename="ok.png", width=256, height=256, _output_dir=d)
        assert os.path.exists(res["png"]), "no PNG produced"
        assert res["svg"] and os.path.exists(res["svg"]), "expected an SVG twin from vector ops"
        assert res["meta"]["format"] == "json"


def test_render_json_accepts_a_string():
    with tempfile.TemporaryDirectory() as d:
        res = renderer.render_json(json.dumps(VALID), filename="s.png", width=128, height=128, _output_dir=d)
        assert os.path.exists(res["png"])


def test_invalid_json_string_is_rejected():
    raised = False
    with tempfile.TemporaryDirectory() as d:
        try:
            renderer.render_json("{ not json", filename="x.png", width=64, height=64, _output_dir=d)
        except renderer.SceneValidationError:
            raised = True
    assert raised, "malformed JSON should raise SceneValidationError"


def test_grain_count_is_clamped_not_unbounded():
    # A scene under the validation cap but with a large grain count must still
    # render in bounded time — the painter clamps to MAX_GRAIN.
    scene = {"layers": [{"ops": [{"op": "grain", "count": 10 ** 9, "fill": [255, 255, 255]}]}]}
    assert scene_json.validate_scene_json(scene) is None
    with tempfile.TemporaryDirectory() as d:
        res = renderer.render_json(scene, filename="g.png", width=64, height=64, _output_dir=d)
        assert os.path.exists(res["png"])


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
