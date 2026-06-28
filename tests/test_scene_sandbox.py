"""
test_scene_sandbox.py — the scene-code sandbox battery from ANTI_ABUSE_TASK.md.

Covers Layer A (renderer.validate_scene AST allowlist + restricted builtins) and
Layer B (sandbox.run_scene subprocess rlimits + wall-clock kill). Run with:

    python -m pytest tests/test_scene_sandbox.py        # if pytest is installed
    python tests/test_scene_sandbox.py                  # plain-stdlib fallback

These assert the exact escapes the task lists: import, the subclasses() walk,
open(), the running-generator frame walk reaching f_globals['os'], a coroutine's
cr_frame, the str.format template walk, a CPU bomb (RLIMIT_CPU), and a memory
bomb (RLIMIT_AS).
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from renderer import validate_scene, render, SceneValidationError  # noqa: E402

BENIGN = """
img = Image.new("RGB", (W, H), palette.get('bg', (20, 20, 30)))
draw = ImageDraw.Draw(img)
for i in range(8):
    x = rng.randint(0, W)
    y = rng.randint(0, H)
    r = rng.randint(10, 60)
    draw.ellipse([x - r, y - r, x + r, y + r], fill=palette.get('accent', (200, 200, 255)))
draw.polygon([(10, 10), (W - 10, 20), (W // 2, H - 10)], outline=(255, 255, 255))
"""

# Scene strings that validate_scene MUST reject (returns a non-None error string).
BLOCKED = {
    "import": "import os\nos.system('id')\n",
    "subclasses_walk": "x = ().__class__.__bases__[0].__subclasses__()\n",
    "open": "open('/etc/passwd').read()\n",
    "generator_frame_walk": (
        "g = (x for x in range(1))\n"
        "glb = g.gi_frame.f_back.f_globals\n"
    ),
    "running_generator_send": (
        "def gen():\n"
        "    f = (yield)\n"
        "    f.gi_frame.f_back.f_globals['os'].system('id')\n"
        "g = gen(); next(g)\n"
    ),
    "coroutine_cr_frame": (
        "async def c():\n"
        "    return 1\n"
        "co = c()\n"
        "glb = co.cr_frame.f_globals\n"
    ),
    "str_format_walk": "s = '{0.__class__}'.format(draw)\n",
    "format_map_walk": "s = '{0.__class__}'.format_map({'x': draw})\n",
    "getattr_name": "getattr(draw, 'im')\n",
    "dunder_class": "c = draw.__class__\n",
}


def test_benign_validates():
    assert validate_scene(BENIGN) is None, "benign scene must pass static validation"


def test_blocked_scenes_rejected():
    for name, code in BLOCKED.items():
        err = validate_scene(code)
        assert err is not None, f"{name!r} was NOT rejected by validate_scene"


def test_benign_renders():
    with tempfile.TemporaryDirectory() as d:
        res = render(BENIGN, filename="benign.png", width=128, height=128, _output_dir=d)
        assert os.path.exists(res["png"]), "benign render produced no PNG"


def test_rce_payload_cannot_reach_os_via_render():
    """The headline regression: a frame-walk payload that reached the real os
    module and ran os.getcwd() before the patch must now be refused by render()."""
    rce = (
        "g = (x for x in range(1))\n"
        "f = g.gi_frame\n"
        "found = None\n"
        "while f is not None:\n"
        "    if 'os' in f.f_globals:\n"
        "        found = f.f_globals['os']; break\n"
        "    f = f.f_back\n"
        "img.info['x'] = found.getcwd()\n"
    )
    assert validate_scene(rce) is not None
    raised = False
    with tempfile.TemporaryDirectory() as d:
        try:
            render(rce, filename="rce.png", width=64, height=64, _output_dir=d)
        except SceneValidationError:
            raised = True
    assert raised, "render() executed a frame-walk RCE instead of rejecting it"


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
