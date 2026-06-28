"""
scene_json.py — declarative JSON scene interpreter (SCENE_FORMAT=json).

The default renderer EXECUTES model-generated Python ("scene code"), which is a
prompt-injection -> RCE surface contained only by the sandbox stack (AST
allowlist + restricted builtins + subprocess + network-less worker). This module
is the safe-by-construction alternative: the model returns a JSON *description* of
the scene, which is validated against a fixed schema and drawn with a fixed set of
Pillow primitives. There is no exec, no eval, and no attribute access — so the
entire RCE class simply does not exist for this path.

What's left is a *data* threat model, not a code one: a scene that asks for an
enormous number of shapes/points or a giant grain field is a DoS, nothing more.
Those are bounded by the caps below (and, in deployment, by sandbox.py's CPU/
memory/wall-clock limits as defense-in-depth around Pillow itself).

Schema (see SYSTEM_PROMPT_JSON in app.py for the model-facing description):

    {
      "background": {"type": "gradient", "from": [r,g,b], "to": [r,g,b],
                     "direction": "vertical"},        # or {"type":"solid","color":[r,g,b]}
      "layers": [
        {"alpha": 255, "ops": [
            {"op": "polygon",   "points": [[x,y],...], "fill": <color>, "outline": <color>, "width": 1},
            {"op": "ellipse",   "bbox": [x0,y0,x1,y1], "fill": <color>, "outline": <color>, "width": 1},
            {"op": "rectangle", "bbox": [x0,y0,x1,y1], "fill": <color>},
            {"op": "line",      "points": [[x,y],...], "fill": <color>, "width": 2},
            {"op": "arc",       "bbox": [...], "start": 0, "end": 180, "fill": <color>, "width": 2},
            {"op": "point",     "points": [[x,y],...], "fill": <color>},
            {"op": "text",      "xy": [x,y], "text": "hi", "fill": <color>, "size": 14},
            {"op": "grain",     "count": 2000, "fill": <color>, "alpha": 40},
            {"op": "vignette",  "strength": 85}
        ]}
      ]
    }

<color> is [r,g,b] or [r,g,b,a] (0-255) or one of the palette keys
"bg"/"atmosphere"/"accent".
"""

from PIL import Image, ImageDraw, ImageFont

# ── DoS caps (the JSON carries no code, so these *are* the threat model) ──────
MAX_LAYERS  = 64
MAX_OPS     = 5000
MAX_POINTS  = 2000
MAX_GRAIN   = 20000
MAX_TEXT    = 500
_COORD_CLAMP = 100_000

ALLOWED_OPS = frozenset({
    "polygon", "ellipse", "rectangle", "line", "arc", "point", "text",
    "grain", "vignette",
})

# Palette keys whose values are colors (palette["grain"] is a count, not a color).
_PALETTE_COLOR_KEYS = ("bg", "atmosphere", "accent")

# Font candidates (mirrors the Python path; falls back to Pillow's default).
_FONT_PATHS = (
    "/usr/share/fonts/truetype/google-fonts/Poppins-Light.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)


# ── validation ───────────────────────────────────────────────────────────────
def validate_scene_json(scene):
    """Return an error string if the scene violates the schema/caps, else None.
    Cheap; runs before any drawing (and gates a model retry, like the Python
    path's validate_scene)."""
    if not isinstance(scene, dict):
        return "scene must be a JSON object"

    bg = scene.get("background")
    if bg is not None:
        if not isinstance(bg, dict):
            return "'background' must be an object"
        if bg.get("type") not in (None, "solid", "gradient"):
            return f"unknown background type {bg.get('type')!r}"

    layers = scene.get("layers")
    if layers is None:
        return "scene must have a 'layers' list"
    if not isinstance(layers, list):
        return "'layers' must be a list"
    if len(layers) > MAX_LAYERS:
        return f"too many layers ({len(layers)} > {MAX_LAYERS})"

    total_ops = 0
    for li, layer in enumerate(layers):
        if not isinstance(layer, dict):
            return f"layer {li} must be an object"
        ops = layer.get("ops", [])
        if not isinstance(ops, list):
            return f"layer {li} 'ops' must be a list"
        total_ops += len(ops)
        if total_ops > MAX_OPS:
            return f"too many ops total (> {MAX_OPS})"
        for oi, op in enumerate(ops):
            if not isinstance(op, dict):
                return f"op {li}.{oi} must be an object"
            name = op.get("op")
            if name not in ALLOWED_OPS:
                return f"unknown op {name!r} (allowed: {', '.join(sorted(ALLOWED_OPS))})"
            pts = op.get("points")
            if pts is not None:
                if not isinstance(pts, list):
                    return f"op {li}.{oi} 'points' must be a list"
                if len(pts) > MAX_POINTS:
                    return f"op {li}.{oi} has too many points ({len(pts)} > {MAX_POINTS})"
            if name == "text":
                t = op.get("text", "")
                if not isinstance(t, str):
                    return f"op {li}.{oi} 'text' must be a string"
                if len(t) > MAX_TEXT:
                    return f"op {li}.{oi} text too long (> {MAX_TEXT})"
    return None


# ── coercion helpers (defensive: even post-validation, never trust the numbers) ─
def _num(v, default=0.0):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return max(-_COORD_CLAMP, min(_COORD_CLAMP, n))


def _ipt(p):
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        return (0, 0)
    return (int(_num(p[0])), int(_num(p[1])))


def _points(op, key="points"):
    raw = op.get(key) or []
    if not isinstance(raw, list):
        return []
    return [_ipt(p) for p in raw[:MAX_POINTS]]


def _bbox(op):
    b = op.get("bbox") or [0, 0, 0, 0]
    if not isinstance(b, (list, tuple)):
        b = [0, 0, 0, 0]
    b = [int(_num(x)) for x in (list(b) + [0, 0, 0, 0])[:4]]
    x0, y0, x1, y1 = b
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _width(op, default=1):
    return max(1, int(_num(op.get("width", default), default)))


def _color(c, palette, default=None):
    """Resolve a color: an [r,g,b(,a)] list, a palette key string, or default."""
    if isinstance(c, str):
        v = palette.get(c) if c in _PALETTE_COLOR_KEYS else None
        c = v
    if isinstance(c, (list, tuple)) and 3 <= len(c) <= 4:
        return tuple(int(max(0, min(255, _num(x)))) for x in c)
    return default


_font_cache = {}


def _font(size):
    size = max(4, min(400, int(_num(size, 14))))
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in _FONT_PATHS:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


# ── per-op drawing ───────────────────────────────────────────────────────────
def _draw_op(op, draw, svg, palette, W, H, rng):
    name = op.get("op")

    if name == "polygon":
        pts = _points(op)
        if len(pts) < 2:
            return
        fill = _color(op.get("fill"), palette)
        outline = _color(op.get("outline"), palette)
        w = _width(op)
        draw.polygon(pts, fill=fill, outline=outline)
        if outline and w > 1:                       # polygon() ignores width pre-Pillow 9.1
            draw.line(pts + [pts[0]], fill=outline, width=w)
        if svg is not None:
            svg.polygon(pts, fill=fill, outline=outline, width=w)

    elif name == "ellipse":
        bb = _bbox(op)
        fill = _color(op.get("fill"), palette)
        outline = _color(op.get("outline"), palette)
        w = _width(op)
        draw.ellipse(bb, fill=fill, outline=outline, width=w)
        if svg is not None:
            svg.ellipse(bb, fill=fill, outline=outline, width=w)

    elif name == "rectangle":
        bb = _bbox(op)
        fill = _color(op.get("fill"), palette)
        outline = _color(op.get("outline"), palette)
        w = _width(op)
        draw.rectangle(bb, fill=fill, outline=outline, width=w)
        if svg is not None:
            svg.rectangle(bb, fill=fill, outline=outline, width=w)

    elif name == "line":
        pts = _points(op)
        if len(pts) < 2:
            return
        fill = _color(op.get("fill"), palette, default=(255, 255, 255))
        w = _width(op)
        draw.line(pts, fill=fill, width=w)
        if svg is not None:
            svg.line(pts, fill=fill, width=w)

    elif name == "arc":
        bb = _bbox(op)
        start = _num(op.get("start", 0))
        end = _num(op.get("end", 360))
        fill = _color(op.get("fill"), palette, default=(255, 255, 255))
        w = _width(op)
        draw.arc(bb, start=start, end=end, fill=fill, width=w)
        if svg is not None:
            svg.arc(bb, start, end, fill=fill, width=w)

    elif name == "point":
        pts = _points(op)
        fill = _color(op.get("fill"), palette, default=(255, 255, 255))
        for xy in pts:
            draw.point(xy, fill=fill)
        if svg is not None:
            svg.point(pts, fill=fill)

    elif name == "text":
        xy = _ipt(op.get("xy", [0, 0]))
        text = str(op.get("text", ""))[:MAX_TEXT]
        fill = _color(op.get("fill"), palette, default=(255, 255, 255))
        font = _font(op.get("size", 14))
        draw.text(xy, text, fill=fill, font=font)
        if svg is not None:
            svg.text(xy, text, fill=fill, font=font)

    elif name == "grain":
        _grain(op, draw, palette, W, H, rng)

    elif name == "vignette":
        _vignette(draw, W, H, op.get("strength", 85))


def _grain(op, draw, palette, W, H, rng):
    count = max(0, min(MAX_GRAIN, int(_num(op.get("count", 1000), 1000))))
    col = _color(op.get("fill"), palette, default=(255, 255, 255))
    a = int(max(0, min(255, _num(op.get("alpha", 40), 40))))
    if len(col) == 3:
        col = (col[0], col[1], col[2], a)
    if W < 1 or H < 1:
        return
    for _ in range(count):
        draw.point((rng.randint(0, W - 1), rng.randint(0, H - 1)), fill=col)


def _vignette(draw, W, H, strength):
    strength = int(max(0, min(255, _num(strength, 85))))
    m = min(W, H) // 2 or 1
    for r in range(0, m, 10):
        a = int(strength * (r / m))
        draw.rectangle([r, r, W - r, H - r], outline=(0, 0, 0, a), width=10)


def _paint_background(bg, img, palette, W, H):
    t = bg.get("type")
    if t == "gradient":
        c0 = _color(bg.get("from"), palette, default=(10, 10, 20))
        c1 = _color(bg.get("to"), palette, default=(30, 30, 50))
        horizontal = bg.get("direction") == "horizontal"
        d = ImageDraw.Draw(img)
        n = (W if horizontal else H) or 1
        for i in range(n):
            f = i / (n - 1) if n > 1 else 0.0
            col = (int(c0[0] + (c1[0] - c0[0]) * f),
                   int(c0[1] + (c1[1] - c0[1]) * f),
                   int(c0[2] + (c1[2] - c0[2]) * f))
            if horizontal:
                d.line([(i, 0), (i, H)], fill=col)
            else:
                d.line([(0, i), (W, i)], fill=col)
    elif t == "solid":
        c = _color(bg.get("color"), palette, default=(0, 0, 0))
        ImageDraw.Draw(img).rectangle([0, 0, W, H], fill=c[:3])
    # else: leave the canvas as created (e.g. the preset background color)


# ── entry point ──────────────────────────────────────────────────────────────
def paint(scene, *, img, draw, svg, W, H, rng, palette):
    """Draw a validated JSON scene onto `img`. Returns the final image (layer
    compositing replaces it). A single malformed op is skipped, not fatal —
    mirroring the way one bad draw call wouldn't abort the Python path."""
    _paint_background(scene.get("background") or {}, img, palette, W, H)

    for layer in scene.get("layers", []):
        ops = layer.get("ops", []) or []
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for op in ops:
            try:
                _draw_op(op, odraw, svg, palette, W, H, rng)
            except Exception:
                continue   # one bad op shouldn't kill the whole render
        a = int(max(0, min(255, _num(layer.get("alpha", 255), 255))))
        if a < 255:
            scaled = overlay.split()[3].point(lambda v: v * a // 255)
            overlay.putalpha(scaled)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    return img
