"""
renderer.py — Pillow Polygons Renderer v2
Executes scene_code strings produced by the pillow-polygons skill.
Outputs PNG + SVG twin + JSON sidecar + thumbnail.
"""

from PIL import Image, ImageDraw, ImageFont
import ast, math, random, sys, os, json
from datetime import datetime

OUTPUT_DIR = "/mnt/user-data/outputs"


# ── scene-code sandbox, part A: static validation + restricted builtins ──────
# Claude-generated scene code is executed (see _exec_code). Because the code is
# produced from a user-controlled prompt, that is a prompt-injection -> RCE
# surface. Part A locks down *what the code can do*; sandbox.py (part B) locks
# down *what resources it can consume* by running this in a subprocess.
#
# Defense 1: an AST allowlist that rejects imports, dunder access (the usual
#   `().__class__.__subclasses__()` escape), and dangerous builtin names.
# Defense 2: exec with a curated __builtins__ — no __import__/open/eval/exec, so
#   even code that slips past the AST scan can't reach os/socket/filesystem.

class SceneValidationError(Exception):
    """Raised when scene code uses a construct the sandbox forbids."""


# Builtin *names* that must never appear in scene code.
_DENIED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "hasattr",
    "memoryview", "help", "exit", "quit", "copyright", "credits", "license",
})

# Attribute names that must never be accessed, even though they don't start with
# an underscore. These are the CPython frame/generator/coroutine/traceback
# introspection attributes — the second known escape class after dunders.
# A *running* generator/coroutine exposes its execution frame without any dunder:
#     g = (x for x in range(1)); g.gi_frame.f_back.f_globals['os']
# walks out of the restricted exec frame into renderer.py's own globals (which
# DO import os/sys), reaching real modules with no `__`/`import`/builtin in sight.
# We block the whole family by prefix so a future attr we didn't enumerate is
# still caught; none of the injected drawing objects use these prefixes.
_DENIED_ATTR_PREFIXES = ("f_", "gi_", "cr_", "ag_", "tb_", "func_")

# Method names that walk attributes from a format string at runtime, e.g.
#     '{0.__class__.__bases__}'.format(draw)   /   '{0.gi_frame}'.format_map(...)
# The dunders/introspection attrs live inside a *string literal*, so the AST scan
# above never sees them — deny the entry points instead.
_DENIED_ATTRS = frozenset({"format", "format_map"})

# The only builtins scene code is allowed to use. No __import__/open/eval/exec.
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "bytearray", "bytes", "chr", "complex", "dict",
    "divmod", "enumerate", "filter", "float", "format", "frozenset", "hex", "int",
    "isinstance", "issubclass", "iter", "len", "list", "map", "max", "min", "next",
    "ord", "pow", "print", "range", "repr", "reversed", "round", "set", "slice",
    "sorted", "str", "sum", "tuple", "zip",
    # exceptions scene code may reference (the prompt tells it to use try/except)
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "OSError",
    "IOError", "AttributeError", "ZeroDivisionError", "RuntimeError", "StopIteration",
    "True", "False", "None",
)


def _build_safe_builtins():
    import builtins
    safe = {}
    for name in _SAFE_BUILTIN_NAMES:
        if hasattr(builtins, name):
            safe[name] = getattr(builtins, name)
    return safe


SAFE_BUILTINS = _build_safe_builtins()


def validate_scene(code):
    """Static-analyze one scene-code string. Returns an error string if it uses a
    forbidden construct, else None. Cheap enough to run before exec (and to gate
    a Claude retry on, like the syntax check)."""
    if not isinstance(code, str):
        return "scene code must be a string"
    try:
        tree = ast.parse(code, "<scene>", "exec")
    except SyntaxError as e:
        return f"syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "imports are not allowed in scene code"
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("_"):
                # blocks __class__, __globals__, __subclasses__, __builtins__, etc.
                return f"access to attribute '{attr}' is not allowed"
            if attr.startswith(_DENIED_ATTR_PREFIXES):
                # frame/generator/coroutine/traceback introspection (gi_frame,
                # f_back, f_globals, cr_frame, tb_frame, …) — the non-dunder escape.
                return f"access to introspection attribute '{attr}' is not allowed"
            if attr in _DENIED_ATTRS:
                # str.format / format_map walk attributes from the template string.
                return f"access to attribute '{attr}' is not allowed"
        if isinstance(node, ast.Name):
            if node.id in _DENIED_NAMES:
                return f"use of '{node.id}' is not allowed"
            if node.id.startswith("__") and node.id.endswith("__"):
                return f"use of '{node.id}' is not allowed"
    return None

# One source of truth for presets: palette colors PLUS the human-facing label and
# an honest, literal description of what the theme does to the sky. The UI renders
# its buttons and popovers from this list (so they can't drift from the renderer),
# and PRESETS below — colors only, what the renderer/palette consumes — is derived
# from it. Descriptions state brightness plainly (e.g. golden is dusk, not midday)
# so the picker doesn't let users assume the wrong thing.
_PALETTE_KEYS = ("bg", "atmosphere", "accent", "grain")
PRESET_INFO = [
    {"name": "night",  "label": "Night",
     "description": "Deep near-black night in cool blues — starlit, low-light mood.",
     "bg": (8,10,22),    "atmosphere": (15,20,45,60),   "accent": (180,210,255), "grain": 3000},
    {"name": "golden", "label": "Golden",
     "description": "Warm golden-hour dusk: a dark amber sky. Not a bright daytime look.",
     "bg": (38,28,12),   "atmosphere": (80,55,20,50),   "accent": (240,195,80),  "grain": 2000},
    {"name": "swamp",  "label": "Swamp",
     "description": "Murky low-light bog — muted greens, dim and moody.",
     "bg": (8,18,12),    "atmosphere": (20,50,28,55),   "accent": (70,160,90),   "grain": 2500},
    {"name": "bone",   "label": "Bone",
     "description": "Pale bleached daylight — a light off-white / tan background.",
     "bg": (210,200,185),"atmosphere": (180,168,148,40),"accent": (90,70,50),    "grain": 1500},
    {"name": "day",    "label": "Day",
     "description": "Bright blue midday sky, light and airy — the sunny-day look.",
     "bg": (206,231,250),"atmosphere": (120,175,235,50),"accent": (255,224,130), "grain": 700},
    {"name": "dawn",   "label": "Dawn",
     "description": "Soft sunrise — peach horizon fading to light periwinkle. Warm and gentle.",
     "bg": (255,206,178),"atmosphere": (150,140,200,55),"accent": (255,168,120), "grain": 1200},
    {"name": "storm",  "label": "Storm",
     "description": "Grey overcast — flat, muted, low-contrast slate.",
     "bg": (120,126,136),"atmosphere": (86,92,102,60),  "accent": (206,212,222), "grain": 2200},
    {"name": "neon",   "label": "Neon",
     "description": "Saturated retro glow — violet-to-magenta sky with a cyan accent.",
     "bg": (232,48,150), "atmosphere": (70,18,110,60),  "accent": (72,232,226),  "grain": 1200},
]
PRESETS = {p["name"]: {k: p[k] for k in _PALETTE_KEYS} for p in PRESET_INFO}


class SVGRecorder:
    """
    Drop-in alongside ImageDraw that records drawing calls as SVG elements.
    Passed into scene_code as `svg`. Scene code calls both draw.X and svg.X
    for full vector fidelity. If scene code doesn't call svg, PNG-only output.
    """
    def __init__(self, width, height):
        self.W = width
        self.H = height
        self._elems = []
        self._defs  = []

    def _col(self, c):
        if c is None: return "none"
        if isinstance(c, str): return c
        if len(c) == 4: return f"rgba({c[0]},{c[1]},{c[2]},{c[3]/255:.3f})"
        return f"rgb({c[0]},{c[1]},{c[2]})"

    def polygon(self, xy, fill=None, outline=None, width=1):
        pts = " ".join(f"{x},{y}" for x,y in xy)
        stroke = f'stroke="{self._col(outline)}" stroke-width="{width}"' if outline else 'stroke="none"'
        self._elems.append(f'<polygon points="{pts}" fill="{self._col(fill)}" {stroke}/>')

    def ellipse(self, xy, fill=None, outline=None, width=1):
        x0,y0,x1,y1 = xy
        cx,cy = (x0+x1)/2, (y0+y1)/2
        rx,ry = abs(x1-x0)/2, abs(y1-y0)/2
        stroke = f'stroke="{self._col(outline)}" stroke-width="{width}"' if outline else 'stroke="none"'
        self._elems.append(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" fill="{self._col(fill)}" {stroke}/>')

    def rectangle(self, xy, fill=None, outline=None, width=1):
        x0,y0,x1,y1 = xy
        # Normalize an inverted box so <rect> never gets a negative width/height
        # (mirrors the PNG path's bbox guard, and the abs() ellipse/arc already use).
        x0,x1 = min(x0,x1),max(x0,x1)
        y0,y1 = min(y0,y1),max(y0,y1)
        stroke = f'stroke="{self._col(outline)}" stroke-width="{width}"' if outline else 'stroke="none"'
        self._elems.append(f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="{self._col(fill)}" {stroke}/>')

    def line(self, xy, fill=None, width=1):
        if len(xy) == 2:
            (x0,y0),(x1,y1) = xy
            self._elems.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{self._col(fill)}" stroke-width="{width}"/>')
        else:
            pts = " ".join(f"{x},{y}" for x,y in xy)
            self._elems.append(f'<polyline points="{pts}" stroke="{self._col(fill)}" stroke-width="{width}" fill="none"/>')

    def arc(self, xy, start, end, fill=None, width=1):
        x0,y0,x1,y1 = xy
        cx,cy = (x0+x1)/2,(y0+y1)/2
        rx,ry = abs(x1-x0)/2,abs(y1-y0)/2
        import math as _m
        s = _m.radians(start); e = _m.radians(end)
        x1s = cx + rx*_m.cos(s); y1s = cy + ry*_m.sin(s)
        x2e = cx + rx*_m.cos(e); y2e = cy + ry*_m.sin(e)
        large = 1 if (end - start) > 180 else 0
        self._elems.append(
            f'<path d="M {x1s:.1f} {y1s:.1f} A {rx:.1f} {ry:.1f} 0 {large} 1 {x2e:.1f} {y2e:.1f}" '
            f'stroke="{self._col(fill)}" stroke-width="{width}" fill="none"/>'
        )

    def point(self, xy, fill=None):
        for x,y in xy:
            self._elems.append(f'<circle cx="{x}" cy="{y}" r="1" fill="{self._col(fill)}"/>')

    def text(self, xy, text, fill=None, font=None):
        x,y = xy
        size = getattr(font, 'size', 14) if font else 14
        self._elems.append(
            f'<text x="{x}" y="{y+size}" font-size="{size}" fill="{self._col(fill)}" '
            f'font-family="sans-serif">{text}</text>'
        )

    def path(self, d, fill=None, stroke=None, width=1):
        """A raw SVG path (used for bezier curves). `d` is a pre-built path string."""
        s = f'stroke="{self._col(stroke)}" stroke-width="{width}"' if stroke else 'stroke="none"'
        self._elems.append(f'<path d="{d}" fill="{self._col(fill)}" {s}/>')

    def mark(self):
        """Index of the next element — pair with group_opacity() to wrap a range."""
        return len(self._elems)

    def group_opacity(self, start, opacity):
        """Wrap the elements recorded since `start` in a <g opacity="..."> so a
        translucent JSON layer renders the same in the SVG twin as in the PNG
        (otherwise those elements were recorded at full opacity)."""
        if opacity >= 1 or start >= len(self._elems):
            return
        inner = "".join(self._elems[start:])
        self._elems[start:] = [f'<g opacity="{max(0.0, opacity):.3f}">{inner}</g>']

    def linear_gradient_bg(self, c0, c1, horizontal=False):
        """Register a linear-gradient def and fill the whole canvas with it — one
        vector element regardless of canvas size (resolution-independent, ~0 cost)."""
        gid = f"g{len(self._defs)}"
        x2, y2 = (1, 0) if horizontal else (0, 1)
        self._defs.append(
            f'<linearGradient id="{gid}" x1="0" y1="0" x2="{x2}" y2="{y2}">'
            f'<stop offset="0" stop-color="{self._col(c0)}"/>'
            f'<stop offset="1" stop-color="{self._col(c1)}"/></linearGradient>')
        self._elems.append(f'<rect width="{self.W}" height="{self.H}" fill="url(#{gid})"/>')

    def radial_gradient_bg(self, inner, outer, cx, cy, r):
        gid = f"g{len(self._defs)}"
        self._defs.append(
            f'<radialGradient id="{gid}" cx="{cx/self.W:.3f}" cy="{cy/self.H:.3f}" '
            f'r="{r/max(self.W,self.H):.3f}">'
            f'<stop offset="0" stop-color="{self._col(inner)}"/>'
            f'<stop offset="1" stop-color="{self._col(outer)}"/></radialGradient>')
        self._elems.append(f'<rect width="{self.W}" height="{self.H}" fill="url(#{gid})"/>')

    def to_svg(self, bg_color=(255,255,255)):
        bg = self._col(bg_color)
        body = "\n  ".join(self._elems)
        defs = ("\n  <defs>" + "".join(self._defs) + "</defs>") if self._defs else ""
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{self.W}" height="{self.H}" viewBox="0 0 {self.W} {self.H}">{defs}\n'
                f'  <rect width="{self.W}" height="{self.H}" fill="{bg}"/>\n'
                f'  {body}\n</svg>')


# ── inverted-bbox guard for the Python path ──────────────────────────────────
# Pillow raises "x1 must be greater than or equal to x0" when a box-based op
# (ellipse/arc/pieslice/chord/rounded_rectangle — and rectangle on older Pillow)
# is handed an inverted box (x1<x0 or y1<y0). Model scene code routinely computes
# boxes as center±radius or from two unordered points, so an inverted box is a
# routine model slip, not abuse — it shouldn't hard-fail an entire render. We
# normalize the box (min/max) exactly as the JSON path's _bbox already does, so
# the shape just draws. Only the Python render() path needs this; render_json()
# is already covered by scene_json._bbox.
_BBOX_METHODS = frozenset({
    "ellipse", "rectangle", "arc", "pieslice", "chord", "rounded_rectangle",
})


def _normalize_bbox(xy):
    """If xy is a bounding box — [x0,y0,x1,y1] or [(x0,y0),(x1,y1)] — return it
    with x0<=x1 and y0<=y1. Anything else (or anything unparseable) is returned
    unchanged, so non-box call shapes pass straight through."""
    try:
        if len(xy) == 4 and all(isinstance(v, (int, float)) for v in xy):
            x0, y0, x1, y1 = xy
        elif len(xy) == 2 and all(len(p) == 2 for p in xy):
            (x0, y0), (x1, y1) = xy
        else:
            return xy
    except (TypeError, ValueError):
        return xy
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


class _SafeDraw:
    """Proxy over a Pillow ImageDraw that normalizes inverted boxes for the
    box-based methods before delegating; every other attribute passes through to
    the wrapped ImageDraw untouched."""
    def __init__(self, draw):
        self._draw = draw

    def __getattr__(self, name):
        attr = getattr(self._draw, name)
        if name in _BBOX_METHODS and callable(attr):
            def wrapped(xy, *args, **kwargs):
                return attr(_normalize_bbox(xy), *args, **kwargs)
            return wrapped
        return attr


class _ImageDrawShim:
    """Stand-in for the ImageDraw module injected into scene code: .Draw() returns
    a bbox-normalizing _SafeDraw (the system prompt has the model re-acquire draw
    via ImageDraw.Draw(img) after every alpha_composite, so wrapping only the
    initial draw wouldn't be enough). Every other name delegates to the real
    module so ImageDraw.ImageDraw, .floodfill, etc. still resolve."""
    def __init__(self, module):
        self._module = module

    def Draw(self, *args, **kwargs):
        return _SafeDraw(self._module.Draw(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._module, name)


def _make_canvas(width, height, preset=None):
    bg = (0,0,0)
    if preset and preset in PRESETS:
        bg = PRESETS[preset]["bg"]
    img  = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    return img, draw


def _exec_code(code, ctx):
    # Validate before executing, then run with a restricted __builtins__ so the
    # code can't import os/socket or reach open/eval/exec. (Belt-and-suspenders
    # with sandbox.py's subprocess + rlimits.)
    err = validate_scene(code)
    if err:
        raise SceneValidationError(err)
    exec(code, ctx)
    return ctx["img"]


def _save_with_sidecar(img, out_path, meta):
    img.save(out_path, quality=95)
    sidecar = out_path.rsplit(".", 1)[0] + ".json"
    with open(sidecar, "w") as f:
        json.dump(meta, f, indent=2)


def render(
    scene_code,
    filename:    str   = "output.png",
    width:       int   = 1024,
    height:      int   = 1024,
    seed:        int   = 42,
    ref:         Image.Image = None,
    preset:      str   = None,
    thumbnail:   bool  = True,
    _output_dir: str   = None,
) -> dict:
    """
    Render a scene. Returns dict with keys: png, svg (or None), thumb, meta.

    Pre-injected into scene_code:
        img, draw, svg, W, H, rng, ref, palette,
        Image, ImageDraw, ImageFont, math, random
    """
    W, H = width, height
    img, draw = _make_canvas(W, H, preset)
    svg_rec   = SVGRecorder(W, H)
    rng       = random.Random(seed)
    _base_palette = {"bg": (20,20,30), "atmosphere": (30,30,50,40), "accent": (200,200,255), "grain": 2000}
    palette   = {**_base_palette, **(PRESETS.get(preset, {}) if preset else {})}

    ctx = {
        # Restricted builtins: no __import__/open/eval/exec/getattr. This replaces
        # the implicit full builtins that a bare exec(code, {}) would inject.
        "__builtins__": SAFE_BUILTINS,
        "img":       img,
        "draw":      _SafeDraw(draw),
        "svg":       svg_rec,
        "W":         W,
        "H":         H,
        "rng":       rng,
        "ref":       ref,
        "palette":   palette,
        "Image":     Image,
        # Shim so ImageDraw.Draw(img) — which the model re-runs after every
        # alpha_composite — yields a bbox-normalizing draw, not a raw one.
        "ImageDraw": _ImageDrawShim(ImageDraw),
        "ImageFont": ImageFont,
        "math":      math,
        "random":    random,
    }

    codes = scene_code if isinstance(scene_code, list) else [scene_code]
    for code in codes:
        img = _exec_code(code, ctx)
        ctx["img"]  = img
        ctx["draw"] = _SafeDraw(ImageDraw.Draw(img))

    meta = {
        "filename":    filename,
        "width":       W,
        "height":      H,
        "seed":        seed,
        "preset":      preset,
        "format":      "python",
        "layers":      len(codes),
        "rendered_at": datetime.utcnow().isoformat() + "Z",
        "scene_code":  codes,
    }
    return _write_outputs(img, svg_rec, filename=filename, out_dir=_output_dir,
                          meta=meta, preset=preset, thumbnail=thumbnail)


def render_json(
    scene,
    filename:    str   = "output.png",
    width:       int   = 1024,
    height:      int   = 1024,
    seed:        int   = 42,
    preset:      str   = None,
    thumbnail:   bool  = True,
    _output_dir: str   = None,
) -> dict:
    """Render a *declarative JSON* scene — the safe-by-construction alternative to
    render(): the scene is data, interpreted by scene_json.paint() with a fixed
    set of Pillow primitives. No exec, so no RCE surface. Same return shape as
    render(). `scene` is a dict or a JSON string.

    Schema invalid -> SceneValidationError (the same type render() raises for a
    rejected scene, so the sandbox/app error paths treat both identically)."""
    import scene_json

    if isinstance(scene, str):
        try:
            scene = json.loads(scene)
        except (ValueError, TypeError) as e:
            raise SceneValidationError(f"scene is not valid JSON: {e}")
    err = scene_json.validate_scene_json(scene)
    if err:
        raise SceneValidationError(err)

    W, H = width, height
    img, _ = _make_canvas(W, H, preset)
    svg_rec = SVGRecorder(W, H)
    rng     = random.Random(seed)
    _base_palette = {"bg": (20,20,30), "atmosphere": (30,30,50,40), "accent": (200,200,255), "grain": 2000}
    palette = {**_base_palette, **(PRESETS.get(preset, {}) if preset else {})}

    img = scene_json.paint(scene, img=img, draw=ImageDraw.Draw(img), svg=svg_rec,
                           W=W, H=H, rng=rng, palette=palette)

    meta = {
        "filename":    filename,
        "width":       W,
        "height":      H,
        "seed":        seed,
        "preset":      preset,
        "format":      "json",
        "layers":      len(scene.get("layers", [])),
        "rendered_at": datetime.utcnow().isoformat() + "Z",
        "scene":       scene,
    }
    return _write_outputs(img, svg_rec, filename=filename, out_dir=_output_dir,
                          meta=meta, preset=preset, thumbnail=thumbnail)


def _write_outputs(img, svg_rec, *, filename, out_dir, meta, preset, thumbnail):
    """Shared output stage for both render paths: PNG + JSON sidecar, an optional
    SVG twin (when the scene emitted vector primitives), and a thumbnail."""
    out_dir  = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)

    _save_with_sidecar(img, out_path, meta)

    svg_path = None
    if svg_rec._elems:
        bg = PRESETS[preset]["bg"] if preset and preset in PRESETS else (255,255,255)
        svg_str  = svg_rec.to_svg(bg_color=bg)
        svg_name = filename.rsplit(".", 1)[0] + ".svg"
        svg_path = os.path.join(out_dir, svg_name)
        with open(svg_path, "w") as f:
            f.write(svg_str)

    thumb_path = None
    if thumbnail:
        thumb = img.copy()
        thumb.thumbnail((256, 256))
        thumb_name = filename.rsplit(".", 1)[0] + "_thumb.png"
        thumb_path = os.path.join(out_dir, thumb_name)
        thumb.save(thumb_path)

    return {"png": out_path, "svg": svg_path, "thumb": thumb_path, "meta": meta}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python renderer.py <scene.py> [output.png] [width] [height] [seed]")
        sys.exit(1)
    with open(sys.argv[1]) as f: code = f.read()
    out  = sys.argv[2] if len(sys.argv) > 2 else "output.png"
    w    = int(sys.argv[3]) if len(sys.argv) > 3 else 1024
    h    = int(sys.argv[4]) if len(sys.argv) > 4 else 1024
    s    = int(sys.argv[5]) if len(sys.argv) > 5 else 42
    result = render(code, out, width=w, height=h, seed=s)
    print(f"PNG:  {result['png']}")
    print(f"SVG:  {result['svg'] or 'none (no svg calls in scene_code)'}")
    print(f"Thumb:{result['thumb']}")
