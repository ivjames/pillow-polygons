"""
renderer.py — Pillow Polygons Renderer v2
Executes scene_code strings produced by the pillow-polygons skill.
Outputs PNG + SVG twin + JSON sidecar + thumbnail.
"""

from PIL import Image, ImageDraw, ImageFont
import math, random, sys, os, json
from datetime import datetime

OUTPUT_DIR = "/mnt/user-data/outputs"

PRESETS = {
    "night":  {"bg": (8,10,22),   "atmosphere": (15,20,45,60),  "accent": (180,210,255), "grain": 3000},
    "golden": {"bg": (38,28,12),  "atmosphere": (80,55,20,50),  "accent": (240,195,80),  "grain": 2000},
    "swamp":  {"bg": (8,18,12),   "atmosphere": (20,50,28,55),  "accent": (70,160,90),   "grain": 2500},
    "bone":   {"bg": (210,200,185),"atmosphere": (180,168,148,40),"accent": (90,70,50),  "grain": 1500},
}


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

    def to_svg(self, bg_color=(255,255,255)):
        bg = self._col(bg_color)
        body = "\n  ".join(self._elems)
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{self.W}" height="{self.H}" viewBox="0 0 {self.W} {self.H}">\n'
                f'  <rect width="{self.W}" height="{self.H}" fill="{bg}"/>\n'
                f'  {body}\n</svg>')


def _make_canvas(width, height, preset=None):
    bg = (0,0,0)
    if preset and preset in PRESETS:
        bg = PRESETS[preset]["bg"]
    img  = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    return img, draw


def _exec_code(code, ctx):
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
    palette   = PRESETS.get(preset, {}) if preset else {}

    ctx = {
        "img":       img,
        "draw":      draw,
        "svg":       svg_rec,
        "W":         W,
        "H":         H,
        "rng":       rng,
        "ref":       ref,
        "palette":   palette,
        "Image":     Image,
        "ImageDraw": ImageDraw,
        "ImageFont": ImageFont,
        "math":      math,
        "random":    random,
    }

    codes = scene_code if isinstance(scene_code, list) else [scene_code]
    for code in codes:
        img = _exec_code(code, ctx)
        ctx["img"]  = img
        ctx["draw"] = ImageDraw.Draw(img)

    out_dir = _output_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)

    meta = {
        "filename":    filename,
        "width":       W,
        "height":      H,
        "seed":        seed,
        "preset":      preset,
        "layers":      len(codes),
        "rendered_at": datetime.utcnow().isoformat() + "Z",
        "scene_code":  codes,
    }
    _save_with_sidecar(img, out_path, meta)

    # SVG output
    svg_path = None
    if svg_rec._elems:
        bg = PRESETS[preset]["bg"] if preset and preset in PRESETS else (255,255,255)
        svg_str  = svg_rec.to_svg(bg_color=bg)
        svg_name = filename.rsplit(".", 1)[0] + ".svg"
        svg_path = os.path.join(out_dir, svg_name)
        with open(svg_path, "w") as f:
            f.write(svg_str)

    # Thumbnail
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
