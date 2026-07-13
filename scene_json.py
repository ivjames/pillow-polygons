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
      "background": {"type": "gradient", "from": [r,g,b], "to": [r,g,b], "direction": "vertical"},
                     # or {"type":"radial","inner":[r,g,b],"outer":[r,g,b],"cx":x,"cy":y,"r":n}
                     # or {"type":"solid","color":[r,g,b]}
      "layers": [
        {"alpha": 255, "ops": [
            {"op": "polygon",   "points": [[x,y],...], "fill": <color>, "outline": <color>, "width": 1},
            {"op": "ellipse",   "bbox": [x0,y0,x1,y1], "fill": <color>, "outline": <color>, "width": 1},
            {"op": "rectangle", "bbox": [x0,y0,x1,y1], "fill": <color>},
            {"op": "line",      "points": [[x,y],...], "fill": <color>, "width": 2},
            {"op": "arc",       "bbox": [...], "start": 0, "end": 180, "fill": <color>, "width": 2},
            {"op": "bezier",    "points": [[x,y]x3or4], "stroke": <color>, "fill": <color>, "width": 2, "closed": false},
            {"op": "point",     "points": [[x,y],...], "fill": <color>},
            {"op": "text",      "xy": [x,y], "text": "hi", "fill": <color>, "size": 14},
            {"op": "grain",     "count": 2000, "fill": <color>, "alpha": 40},
            {"op": "vignette",  "strength": 85},
            # expanding ops — one compact op stamps out many vector shapes (token-cheap):
            {"op": "scatter",   "count": 200, "area": [x0,y0,x1,y1], "shape": <leaf op>},
            {"op": "repeat",    "nx": 8, "ny": 6, "dx": 60, "dy": 60, "x0": 0, "y0": 0, "shape": <leaf op>}
        ]}
      ]
    }

<color> is [r,g,b] or [r,g,b,a] (0-255). All primitives have a vector SVG representation, so
the SVG twin stays faithful.
"""

from PIL import Image, ImageDraw, ImageFont

# ── DoS caps (the JSON carries no code, so these *are* the threat model) ──────
# Two independent budgets: MAX_OPS bounds the number of op *entries* in the JSON
# (i.e. how much the model has to write — token cost); MAX_DRAWS bounds the
# *expanded* primitive count actually rendered, so a compact op like scatter/
# repeat/grain stays token-cheap yet can't turn into millions of draws.
MAX_LAYERS  = 64
MAX_OPS     = 5000
MAX_DRAWS   = 20000
MAX_POINTS  = 2000
MAX_GRAIN   = 20000
MAX_SCATTER = 5000
MAX_TEXT    = 500
MAX_BEZIER_SAMPLES = 64
_COORD_CLAMP = 100_000

# Leaf primitives: things scatter/repeat may stamp out copies of.
LEAF_OPS = frozenset({
    "polygon", "ellipse", "rectangle", "line", "arc", "point", "text", "bezier",
})
# Everything the schema accepts (leaf primitives + the expanding/effect ops).
ALLOWED_OPS = LEAF_OPS | frozenset({"grain", "vignette", "scatter", "repeat"})

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
        if bg.get("type") not in (None, "solid", "gradient", "radial"):
            return f"unknown background type {bg.get('type')!r}"

    layers = scene.get("layers")
    if layers is None:
        return "scene must have a 'layers' list"
    if not isinstance(layers, list):
        return "'layers' must be a list"
    if len(layers) > MAX_LAYERS:
        return f"too many layers ({len(layers)} > {MAX_LAYERS})"

    total_ops = 0
    total_draws = 0
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
            err = _validate_op(op, f"{li}.{oi}", nested=False)
            if err:
                return err
            total_draws += _op_draw_cost(op)
            if total_draws > MAX_DRAWS:
                return f"scene draws too many primitives (> {MAX_DRAWS}); use fewer/smaller scatter/grain/repeat"
    return None


def _validate_op(op, where, nested):
    """Validate one op. `nested` ops (a scatter/repeat template) must be leaves."""
    if not isinstance(op, dict):
        return f"op {where} must be an object"
    name = op.get("op")
    allowed = LEAF_OPS if nested else ALLOWED_OPS
    if name not in allowed:
        kind = "template op" if nested else "op"
        return f"unknown {kind} {name!r} (allowed: {', '.join(sorted(allowed))})"

    pts = op.get("points")
    if pts is not None:
        if not isinstance(pts, list):
            return f"op {where} 'points' must be a list"
        if len(pts) > MAX_POINTS:
            return f"op {where} has too many points ({len(pts)} > {MAX_POINTS})"

    if name == "bezier":
        if not isinstance(pts, list) or len(pts) not in (3, 4):
            return f"op {where} 'bezier' needs 3 (quadratic) or 4 (cubic) control points"

    if name == "text":
        t = op.get("text", "")
        if not isinstance(t, str):
            return f"op {where} 'text' must be a string"
        if len(t) > MAX_TEXT:
            return f"op {where} text too long (> {MAX_TEXT})"

    if name in ("scatter", "repeat"):
        shape = op.get("shape")
        if not isinstance(shape, dict):
            return f"op {where} '{name}' needs a 'shape' template object"
        serr = _validate_op(shape, f"{where}.shape", nested=True)
        if serr:
            return serr
    return None


def _shape_weight(op):
    """Rough per-stamp draw weight of a leaf shape — points dominate cost (each
    stamped copy replays all of them, in both the PNG and the SVG). So scatter/
    repeat must be charged count × this, not just count, or a compact scene could
    stamp thousands of 2000-point polygons and blow past MAX_DRAWS at render time."""
    if not isinstance(op, dict):
        return 1
    name = op.get("op")
    if name == "bezier":
        return MAX_BEZIER_SAMPLES
    if name in ("polygon", "line", "point"):
        pts = op.get("points")
        return max(1, len(pts)) if isinstance(pts, list) else 1
    return 1


def _op_draw_cost(op):
    """Expanded primitive count an op contributes to the MAX_DRAWS budget."""
    name = op.get("op")
    if name == "grain":
        return min(MAX_GRAIN, max(0, int(_num(op.get("count", 0)))))
    if name == "scatter":
        count = min(MAX_SCATTER, max(0, int(_num(op.get("count", 0)))))
        return count * _shape_weight(op.get("shape"))
    if name == "repeat":
        nx = max(0, int(_num(op.get("nx", 0))))
        ny = max(0, int(_num(op.get("ny", 0))))
        return min(MAX_SCATTER, nx * ny) * _shape_weight(op.get("shape"))
    return 1


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


def _color(c, default=None):
    """Resolve a color: an [r,g,b(,a)] list, else the default. (Palette-key color
    strings were removed with the theme system.)"""
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
def _draw_op(op, draw, svg, W, H, rng):
    name = op.get("op")

    if name == "polygon":
        pts = _points(op)
        if len(pts) < 2:
            return
        fill = _color(op.get("fill"))
        outline = _color(op.get("outline"))
        w = _width(op)
        draw.polygon(pts, fill=fill, outline=outline)
        if outline and w > 1:                       # polygon() ignores width pre-Pillow 9.1
            draw.line(pts + [pts[0]], fill=outline, width=w)
        if svg is not None:
            svg.polygon(pts, fill=fill, outline=outline, width=w)

    elif name == "ellipse":
        bb = _bbox(op)
        fill = _color(op.get("fill"))
        outline = _color(op.get("outline"))
        w = _width(op)
        draw.ellipse(bb, fill=fill, outline=outline, width=w)
        if svg is not None:
            svg.ellipse(bb, fill=fill, outline=outline, width=w)

    elif name == "rectangle":
        bb = _bbox(op)
        fill = _color(op.get("fill"))
        outline = _color(op.get("outline"))
        w = _width(op)
        draw.rectangle(bb, fill=fill, outline=outline, width=w)
        if svg is not None:
            svg.rectangle(bb, fill=fill, outline=outline, width=w)

    elif name == "line":
        pts = _points(op)
        if len(pts) < 2:
            return
        fill = _color(op.get("fill"), default=(255, 255, 255))
        w = _width(op)
        draw.line(pts, fill=fill, width=w)
        if svg is not None:
            svg.line(pts, fill=fill, width=w)

    elif name == "arc":
        bb = _bbox(op)
        start = _num(op.get("start", 0))
        end = _num(op.get("end", 360))
        fill = _color(op.get("fill"), default=(255, 255, 255))
        w = _width(op)
        draw.arc(bb, start=start, end=end, fill=fill, width=w)
        if svg is not None:
            svg.arc(bb, start, end, fill=fill, width=w)

    elif name == "point":
        pts = _points(op)
        fill = _color(op.get("fill"), default=(255, 255, 255))
        for xy in pts:
            draw.point(xy, fill=fill)
        if svg is not None:
            svg.point(pts, fill=fill)

    elif name == "text":
        xy = _ipt(op.get("xy", [0, 0]))
        text = str(op.get("text", ""))[:MAX_TEXT]
        fill = _color(op.get("fill"), default=(255, 255, 255))
        font = _font(op.get("size", 14))
        draw.text(xy, text, fill=fill, font=font)
        if svg is not None:
            svg.text(xy, text, fill=fill, font=font)

    elif name == "bezier":
        _bezier(op, draw, svg)

    elif name == "grain":
        _grain(op, draw, W, H, rng)

    elif name == "vignette":
        _vignette(draw, W, H, op.get("strength", 85))

    elif name == "scatter":
        _scatter(op, draw, svg, W, H, rng)

    elif name == "repeat":
        _repeat(op, draw, svg, W, H, rng)


def _bezier(op, draw, svg):
    """A quadratic (3 control points) or cubic (4) bezier. Sampled to a polyline
    for the PNG; emitted as a true vector <path> in the SVG twin. Token-cheap —
    a smooth curve from 3-4 points instead of an enumerated polyline."""
    ctrl = _points(op)
    if len(ctrl) not in (3, 4):
        return
    stroke = _color(op.get("stroke"), default=(255, 255, 255))
    fill = _color(op.get("fill"))
    w = _width(op)
    closed = bool(op.get("closed"))

    n = MAX_BEZIER_SAMPLES
    pts = []
    for i in range(n + 1):
        t = i / n
        if len(ctrl) == 4:
            (x0, y0), (x1, y1), (x2, y2), (x3, y3) = ctrl
            mt = 1 - t
            x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
            y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
        else:
            (x0, y0), (x1, y1), (x2, y2) = ctrl
            mt = 1 - t
            x = mt**2 * x0 + 2 * mt * t * x1 + t**2 * x2
            y = mt**2 * y0 + 2 * mt * t * y1 + t**2 * y2
        pts.append((int(x), int(y)))

    if closed and fill:
        draw.polygon(pts, fill=fill)
    draw.line(pts + ([pts[0]] if closed else []), fill=stroke, width=w)

    if svg is not None:
        if len(ctrl) == 4:
            d = (f"M {ctrl[0][0]} {ctrl[0][1]} C {ctrl[1][0]} {ctrl[1][1]} "
                 f"{ctrl[2][0]} {ctrl[2][1]} {ctrl[3][0]} {ctrl[3][1]}")
        else:
            d = (f"M {ctrl[0][0]} {ctrl[0][1]} Q {ctrl[1][0]} {ctrl[1][1]} "
                 f"{ctrl[2][0]} {ctrl[2][1]}")
        if closed:
            d += " Z"
        svg.path(d, fill=fill if closed else None, stroke=stroke, width=w)


def _translate_op(op, dx, dy):
    """Return a copy of a leaf op with all its coordinates shifted by (dx, dy).
    Used to stamp scatter/repeat copies of a template shape."""
    out = dict(op)
    if "points" in op and isinstance(op["points"], list):
        out["points"] = [[_num(p[0]) + dx, _num(p[1]) + dy]
                         for p in op["points"] if isinstance(p, (list, tuple)) and len(p) >= 2]
    if "bbox" in op and isinstance(op["bbox"], (list, tuple)) and len(op["bbox"]) >= 4:
        b = op["bbox"]
        out["bbox"] = [_num(b[0]) + dx, _num(b[1]) + dy, _num(b[2]) + dx, _num(b[3]) + dy]
    if "xy" in op and isinstance(op["xy"], (list, tuple)) and len(op["xy"]) >= 2:
        out["xy"] = [_num(op["xy"][0]) + dx, _num(op["xy"][1]) + dy]
    return out


def _scatter(op, draw, svg, W, H, rng):
    """Stamp `count` copies of a template `shape` at random offsets within `area`
    (default: the whole canvas). One compact op -> many vector shapes."""
    shape = op.get("shape")
    if not isinstance(shape, dict):
        return
    count = max(0, min(MAX_SCATTER, int(_num(op.get("count", 0)))))
    area = op.get("area")
    if isinstance(area, (list, tuple)) and len(area) >= 4:
        x0, y0, x1, y1 = (_num(area[0]), _num(area[1]), _num(area[2]), _num(area[3]))
    else:
        x0, y0, x1, y1 = 0, 0, W, H
    lo_x, hi_x = int(min(x0, x1)), int(max(x0, x1))
    lo_y, hi_y = int(min(y0, y1)), int(max(y0, y1))
    for _ in range(count):
        dx = rng.randint(lo_x, hi_x) if hi_x > lo_x else lo_x
        dy = rng.randint(lo_y, hi_y) if hi_y > lo_y else lo_y
        _draw_op(_translate_op(shape, dx, dy), draw, svg, W, H, rng)


def _repeat(op, draw, svg, W, H, rng):
    """Stamp a template `shape` across an nx×ny grid stepping by (dx, dy) from
    (x0, y0). One compact op -> a tiled field of vector shapes."""
    shape = op.get("shape")
    if not isinstance(shape, dict):
        return
    nx = max(0, int(_num(op.get("nx", 1))))
    ny = max(0, int(_num(op.get("ny", 1))))
    if nx * ny > MAX_SCATTER:
        return
    dx = _num(op.get("dx", 0))
    dy = _num(op.get("dy", 0))
    x0 = _num(op.get("x0", 0))
    y0 = _num(op.get("y0", 0))
    for j in range(ny):
        for i in range(nx):
            _draw_op(_translate_op(shape, x0 + i * dx, y0 + j * dy),
                     draw, svg, W, H, rng)


def _grain(op, draw, W, H, rng):
    count = max(0, min(MAX_GRAIN, int(_num(op.get("count", 1000), 1000))))
    col = _color(op.get("fill"), default=(255, 255, 255))
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
    # Darken the edges, fade to clear at the center: alpha is highest on the
    # outermost ring (r=0) and 0 at the innermost (r→m). (The ramp used to run the
    # other way, which darkened the *center* — a dark box in the middle.)
    for r in range(0, m, 10):
        a = int(strength * (1 - r / m))
        draw.rectangle([r, r, W - r, H - r], outline=(0, 0, 0, a), width=10)


def _paint_background(bg, img, svg, W, H):
    t = bg.get("type")
    if t == "gradient":
        c0 = _color(bg.get("from"), default=(10, 10, 20))
        c1 = _color(bg.get("to"), default=(30, 30, 50))
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
        if svg is not None:
            svg.linear_gradient_bg(c0, c1, horizontal)
    elif t == "radial":
        inner = _color(bg.get("inner"), default=(60, 60, 90))
        outer = _color(bg.get("outer"), default=(8, 8, 16))
        cx = int(_num(bg.get("cx", W / 2)))
        cy = int(_num(bg.get("cy", H / 2)))
        r = int(_num(bg.get("r", max(W, H) / 2))) or 1
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, H], fill=outer[:3])
        steps = max(1, r // 2)
        for s in range(steps, 0, -1):
            f = s / steps                       # 1 at the edge (outer), ->0 at center (inner)
            col = (int(outer[0] + (inner[0] - outer[0]) * (1 - f)),
                   int(outer[1] + (inner[1] - outer[1]) * (1 - f)),
                   int(outer[2] + (inner[2] - outer[2]) * (1 - f)))
            rr = int(r * f)
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=col)
        if svg is not None:
            svg.radial_gradient_bg(inner, outer, cx, cy, r)
    elif t == "solid":
        c = _color(bg.get("color"), default=(0, 0, 0))
        ImageDraw.Draw(img).rectangle([0, 0, W, H], fill=c[:3])
        if svg is not None:
            svg.rectangle([0, 0, W, H], fill=c[:3])
    # else: leave the canvas as created (its solid fill color)


# ── entry point ──────────────────────────────────────────────────────────────
def paint(scene, *, img, draw, svg, W, H, rng):
    """Draw a validated JSON scene onto `img`. Returns the final image (layer
    compositing replaces it). A single malformed op is skipped, not fatal —
    mirroring the way one bad draw call wouldn't abort the Python path."""
    _paint_background(scene.get("background") or {}, img, svg, W, H)

    for layer in scene.get("layers", []):
        ops = layer.get("ops", []) or []
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        svg_start = svg.mark() if svg is not None else 0
        for op in ops:
            try:
                _draw_op(op, odraw, svg, W, H, rng)
            except Exception:
                continue   # one bad op shouldn't kill the whole render
        a = int(max(0, min(255, _num(layer.get("alpha", 255), 255))))
        if a < 255:
            scaled = overlay.split()[3].point(lambda v: v * a // 255)
            overlay.putalpha(scaled)
            if svg is not None:
                # Mirror the layer fade in the SVG twin so it matches the PNG.
                svg.group_opacity(svg_start, a / 255)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    return img
