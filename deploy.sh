#!/bin/bash
set -e
BASE=/var/www/poly
mkdir -p $BASE/templates $BASE/static/css $BASE/static/js $BASE/static/renders $BASE/static/uploads

cat > $BASE/app.py << 'EOF_APP'
import os, json, sqlite3, uuid, base64, sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template, g
from PIL import Image as PILImage
import anthropic

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RENDERS_DIR = os.path.join(BASE_DIR, "static", "renders")
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads")
DB_PATH     = os.path.join(BASE_DIR, "poly.db")
RENDERER    = os.path.join(BASE_DIR, "renderer.py")

os.makedirs(RENDERS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

# inject renderer into path
sys.path.insert(0, BASE_DIR)
from renderer import render as poly_render

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB upload limit

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── DB ─────────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS images (
            id          TEXT PRIMARY KEY,
            filename    TEXT NOT NULL,
            thumb       TEXT,
            prompt      TEXT,
            preset      TEXT,
            seed        INTEGER,
            width       INTEGER,
            height      INTEGER,
            tokens_in   INTEGER DEFAULT 0,
            tokens_out  INTEGER DEFAULT 0,
            scene_code  TEXT,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS folders (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS tags (
            id   TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS image_folders (
            image_id  TEXT,
            folder_id TEXT,
            PRIMARY KEY (image_id, folder_id)
        );
        CREATE TABLE IF NOT EXISTS image_tags (
            image_id TEXT,
            tag_id   TEXT,
            PRIMARY KEY (image_id, tag_id)
        );
        """)
    print("DB initialised")

init_db()

# ── helpers ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the Pillow Polygons scene code generator.

Given a user prompt (and optionally a reference image), output ONLY a Python code string — no markdown, no backticks, no explanation. Just raw Python drawing instructions.

The following are pre-injected and available without importing:
  img, draw, W, H, rng, ref, palette,
  Image, ImageDraw, ImageFont, math, random

Rules:
- Use rng (not random) for all randomness
- After every alpha_composite, re-acquire draw:
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    draw = ImageDraw.Draw(img)
- Always end with a vignette:
    vig = Image.new("RGBA",(W,H),(0,0,0,0))
    vd = ImageDraw.Draw(vig)
    for r in range(0,min(W,H)//2,10):
        a = int(85*(r/(min(W,H)//2)))
        vd.rectangle([r,r,W-r,H-r], outline=(0,0,0,a), width=10)
    img = Image.alpha_composite(img.convert("RGBA"),vig).convert("RGB")
    draw = ImageDraw.Draw(img)
- Use gradient backgrounds (scan line by line)
- Build characters from polygons and ellipses with shadow/base/highlight layers
- Eyes need socket → iris → pupil → gleam
- If ref is provided, use ref.getpixel((x,y)) to sample dominant colors

Available presets inject palette dict with keys: bg, atmosphere, accent, grain
Available fonts (use try/except):
  /usr/share/fonts/truetype/google-fonts/Poppins-Light.ttf
  /usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf
  /usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf

Output raw Python only. Nothing else."""

def image_to_b64(path):
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")

def call_claude(prompt, ref_path=None, preset=None, seed=42):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_content = []

    if ref_path and os.path.exists(ref_path):
        ext = ref_path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "gif": "image/gif",
                "webp": "image/webp"}.get(ext, "image/jpeg")
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": image_to_b64(ref_path)
            }
        })

    preset_note = f"\nActive preset: {preset}" if preset else ""
    seed_note   = f"\nSeed: {seed}"
    user_content.append({
        "type": "text",
        "text": f"{prompt}{preset_note}{seed_note}"
    })

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}]
    )

    scene_code   = response.content[0].text.strip()
    tokens_in    = response.usage.input_tokens
    tokens_out   = response.usage.output_tokens

    return scene_code, tokens_in, tokens_out

def row_to_dict(row):
    d = dict(row)
    db = get_db()
    img_id = d["id"]
    d["tags"] = [r["name"] for r in db.execute(
        "SELECT t.name FROM tags t JOIN image_tags it ON t.id=it.tag_id WHERE it.image_id=?", (img_id,)
    ).fetchall()]
    d["folders"] = [r["name"] for r in db.execute(
        "SELECT f.name FROM folders f JOIN image_folders if2 ON f.id=if2.folder_id WHERE if2.image_id=?", (img_id,)
    ).fetchall()]
    return d

# ── routes: pages ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

# ── routes: generation ─────────────────────────────────────────────────────
@app.route("/api/generate", methods=["POST"])
def generate():
    prompt  = request.form.get("prompt", "").strip()
    preset  = request.form.get("preset") or None
    seed    = int(request.form.get("seed", 42))
    width   = int(request.form.get("width", 1024))
    height  = int(request.form.get("height", 1024))

    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    # handle optional ref image
    ref_path = None
    ref_pil  = None
    if "ref" in request.files and request.files["ref"].filename:
        f        = request.files["ref"]
        ref_name = f"{uuid.uuid4().hex}.jpg"
        ref_path = os.path.join(UPLOADS_DIR, ref_name)
        f.save(ref_path)
        ref_pil = PILImage.open(ref_path).convert("RGB")

    try:
        scene_code, tokens_in, tokens_out = call_claude(prompt, ref_path, preset, seed)
    except Exception as e:
        return jsonify({"error": f"Claude API error: {e}"}), 500

    # render
    img_id   = uuid.uuid4().hex
    filename = f"{img_id}.png"

    try:
        result = poly_render(
            scene_code, filename=filename,
            width=width, height=height, seed=seed,
            ref=ref_pil, preset=preset, thumbnail=True,
            _output_dir=RENDERS_DIR
        )
    except Exception as e:
        return jsonify({"error": f"Render error: {e}", "scene_code": scene_code}), 500

    thumb_name = os.path.basename(result["thumb"]) if result.get("thumb") else None
    svg_name   = os.path.basename(result["svg"])   if result.get("svg")   else None

    created_at = datetime.utcnow().isoformat() + "Z"
    db = get_db()
    db.execute("""
        INSERT INTO images (id, filename, thumb, prompt, preset, seed, width, height,
                            tokens_in, tokens_out, scene_code, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (img_id, filename, thumb_name, prompt, preset, seed, width, height,
          tokens_in, tokens_out, scene_code, created_at))
    db.commit()

    row = db.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    return jsonify({**row_to_dict(row),
                    "url":       f"/static/renders/{filename}",
                    "thumb_url": f"/static/renders/{thumb_name}" if thumb_name else None,
                    "svg_url":   f"/static/renders/{svg_name}"   if svg_name   else None})

# ── routes: images ─────────────────────────────────────────────────────────
@app.route("/api/images")
def list_images():
    db     = get_db()
    q      = request.args.get("q", "").strip()
    folder = request.args.get("folder", "").strip()
    tag    = request.args.get("tag", "").strip()

    sql    = "SELECT DISTINCT i.* FROM images i"
    joins  = []
    wheres = []
    params = []

    if folder:
        joins.append("JOIN image_folders if2 ON i.id=if2.image_id JOIN folders f ON f.id=if2.folder_id")
        wheres.append("f.name=?"); params.append(folder)
    if tag:
        joins.append("JOIN image_tags it ON i.id=it.image_id JOIN tags t ON t.id=it.tag_id")
        wheres.append("t.name=?"); params.append(tag)
    if q:
        wheres.append("i.prompt LIKE ?"); params.append(f"%{q}%")

    if joins:  sql += " " + " ".join(joins)
    if wheres: sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY i.created_at DESC"

    rows = db.execute(sql, params).fetchall()
    return jsonify([{**row_to_dict(r),
                     "url": f"/static/renders/{r['filename']}",
                     "thumb_url": f"/static/renders/{r['thumb']}"} for r in rows])

@app.route("/api/images/<img_id>", methods=["DELETE"])
def delete_image(img_id):
    db  = get_db()
    row = db.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    if not row: return jsonify({"error": "Not found"}), 404
    for f in [row["filename"], row["thumb"]]:
        p = os.path.join(RENDERS_DIR, f)
        if f and os.path.exists(p): os.remove(p)
    db.execute("DELETE FROM image_tags WHERE image_id=?",   (img_id,))
    db.execute("DELETE FROM image_folders WHERE image_id=?", (img_id,))
    db.execute("DELETE FROM images WHERE id=?",             (img_id,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/images/<img_id>/tags", methods=["POST"])
def add_tag(img_id):
    name = request.json.get("name", "").strip().lower()
    if not name: return jsonify({"error": "Name required"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    tag_id   = existing["id"] if existing else uuid.uuid4().hex
    if not existing:
        db.execute("INSERT INTO tags (id,name) VALUES (?,?)", (tag_id, name))
    try:
        db.execute("INSERT INTO image_tags (image_id,tag_id) VALUES (?,?)", (img_id, tag_id))
    except: pass
    db.commit()
    return jsonify({"ok": True, "tag": name})

@app.route("/api/images/<img_id>/tags/<tag_name>", methods=["DELETE"])
def remove_tag(img_id, tag_name):
    db     = get_db()
    tag    = db.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()
    if tag:
        db.execute("DELETE FROM image_tags WHERE image_id=? AND tag_id=?", (img_id, tag["id"]))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/images/<img_id>/folders", methods=["POST"])
def add_to_folder(img_id):
    name = request.json.get("name", "").strip()
    if not name: return jsonify({"error": "Name required"}), 400
    db = get_db()
    existing  = db.execute("SELECT id FROM folders WHERE name=?", (name,)).fetchone()
    folder_id = existing["id"] if existing else uuid.uuid4().hex
    if not existing:
        db.execute("INSERT INTO folders (id,name) VALUES (?,?)", (folder_id, name))
    try:
        db.execute("INSERT INTO image_folders (image_id,folder_id) VALUES (?,?)", (img_id, folder_id))
    except: pass
    db.commit()
    return jsonify({"ok": True, "folder": name})

@app.route("/api/images/<img_id>/folders/<folder_name>", methods=["DELETE"])
def remove_from_folder(img_id, folder_name):
    db = get_db()
    f  = db.execute("SELECT id FROM folders WHERE name=?", (folder_name,)).fetchone()
    if f:
        db.execute("DELETE FROM image_folders WHERE image_id=? AND folder_id=?", (img_id, f["id"]))
        db.commit()
    return jsonify({"ok": True})

# ── routes: folders & tags ─────────────────────────────────────────────────
@app.route("/api/folders")
def list_folders():
    db = get_db()
    rows = db.execute("""
        SELECT f.name, COUNT(if2.image_id) as count
        FROM folders f LEFT JOIN image_folders if2 ON f.id=if2.folder_id
        GROUP BY f.id ORDER BY f.name
    """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/folders", methods=["POST"])
def create_folder():
    name = request.json.get("name", "").strip()
    if not name: return jsonify({"error": "Name required"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM folders WHERE name=?", (name,)).fetchone()
    if existing: return jsonify({"error": "Folder exists"}), 409
    fid = uuid.uuid4().hex
    db.execute("INSERT INTO folders (id,name) VALUES (?,?)", (fid, name))
    db.commit()
    return jsonify({"ok": True, "id": fid, "name": name})

@app.route("/api/folders/<name>", methods=["DELETE"])
def delete_folder(name):
    db = get_db()
    f  = db.execute("SELECT id FROM folders WHERE name=?", (name,)).fetchone()
    if f:
        db.execute("DELETE FROM image_folders WHERE folder_id=?", (f["id"],))
        db.execute("DELETE FROM folders WHERE id=?", (f["id"],))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/tags")
def list_tags():
    db   = get_db()
    rows = db.execute("""
        SELECT t.name, COUNT(it.image_id) as count
        FROM tags t LEFT JOIN image_tags it ON t.id=it.tag_id
        GROUP BY t.id ORDER BY count DESC, t.name
    """).fetchall()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8040, debug=False)
EOF_APP

cat > $BASE/renderer.py << 'EOF_RENDERER'
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
EOF_RENDERER

cat > $BASE/templates/index.html << 'EOF_HTML'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pillow Polygons</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/css/app.css">
</head>
<body>
  <div class="app">

    <!-- ── BANNER ── -->
    <header class="banner-header">
      <img src="/static/banner.png" alt="Pillow Polygons">
    </header>

    <!-- ── LEFT SIDEBAR: Controls ── -->
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-header">
        <span class="sidebar-logo">⬡ Controls</span>
        <button class="icon-btn" id="toggle-sidebar" title="Collapse">‹</button>
      </div>

      <div class="sidebar-body">
        <div class="field">
          <label>Prompt</label>
          <textarea id="prompt" rows="5" placeholder="Describe your scene… e.g. 'a lighthouse at night in swamp preset'"></textarea>
        </div>

        <div class="field">
          <label>Preset palette</label>
          <div class="preset-grid">
            <button class="preset-btn active" data-preset="">None</button>
            <button class="preset-btn" data-preset="night">Night</button>
            <button class="preset-btn" data-preset="golden">Golden</button>
            <button class="preset-btn" data-preset="swamp">Swamp</button>
            <button class="preset-btn" data-preset="bone">Bone</button>
          </div>
        </div>

        <div class="field row">
          <div class="half">
            <label>Width px</label>
            <input type="number" id="width" value="1024" min="256" max="2048" step="128">
          </div>
          <div class="half">
            <label>Height px</label>
            <input type="number" id="height" value="1024" min="256" max="2048" step="128">
          </div>
        </div>

        <div class="field">
          <label>Seed</label>
          <div class="seed-row">
            <input type="number" id="seed" value="42" min="0" max="99999">
            <button class="icon-btn dark" id="random-seed" title="Random seed">⟳</button>
          </div>
        </div>

        <div class="field">
          <label>Reference image <span class="muted">optional — sampled for color</span></label>
          <div class="upload-zone" id="upload-zone">
            <input type="file" id="ref-input" accept="image/*" hidden>
            <div class="upload-inner" id="upload-inner">
              <span class="upload-icon">⊕</span>
              <span>Drop or click to upload</span>
            </div>
            <img id="ref-preview" class="ref-preview hidden" alt="Reference">
            <button class="remove-ref hidden" id="remove-ref">✕</button>
          </div>
        </div>

        <button class="generate-btn" id="generate-btn">
          <span id="btn-label">Generate</span>
          <span class="spinner hidden" id="spinner">◌</span>
        </button>

        <div class="token-meter hidden" id="token-meter">
          <div class="token-row">
            <span class="token-label">IN</span>
            <span class="token-val in-val" id="t-in">—</span>
          </div>
          <div class="token-row">
            <span class="token-label">OUT</span>
            <span class="token-val out-val" id="t-out">—</span>
          </div>
          <div class="token-row">
            <span class="token-label">TOTAL</span>
            <span class="token-val tot-val" id="t-total">—</span>
          </div>
        </div>

        <div class="error-box hidden" id="error-box"></div>
      </div>
    </aside>

    <!-- ── CENTER: Image display ── -->
    <main class="canvas-area">
      <div class="image-frame" id="image-frame">

        <div class="empty-state" id="empty-state">
          <div class="empty-diamonds">
            <div class="empty-diamond" style="background:#ff3b7a"></div>
            <div class="empty-diamond" style="background:#1a90ff"></div>
            <div class="empty-diamond" style="background:#22dd88"></div>
            <div class="empty-diamond" style="background:#ffe000"></div>
          </div>
          <p>Enter a prompt and hit Generate<br><span style="font-size:11px;opacity:0.6">⌘↵ to generate</span></p>
        </div>

        <img id="result-img" class="result-img hidden" alt="Generated">

        <div class="img-meta hidden" id="img-meta">
          <div class="meta-tags" id="meta-tags"></div>
          <div class="meta-actions">
            <input type="text" id="tag-input" placeholder="Add tag…" class="inline-input" style="width:110px">
            <button class="small-btn" id="add-tag-btn">+ Tag</button>
            <select id="folder-select" class="inline-input">
              <option value="">Add to folder…</option>
            </select>
            <a id="download-btn" class="small-btn" download>↓ PNG</a>
            <a id="svg-download-btn" class="small-btn hidden" download>↓ SVG</a>
            <button class="small-btn danger" id="delete-btn">✕ Delete</button>
          </div>
        </div>

      </div>
    </main>

    <!-- ── RIGHT PANEL: Gallery ── -->
    <aside class="gallery-panel" id="gallery-panel">
      <div class="gallery-header">
        <input type="text" id="search-input" class="search-input" placeholder="Search prompts…">
        <button class="icon-btn" id="toggle-gallery" title="Collapse">›</button>
      </div>

      <div class="folder-bar" id="folder-bar">
        <button class="folder-chip active" data-folder="">All</button>
      </div>

      <div class="tag-bar" id="tag-bar"></div>

      <div class="gallery-grid" id="gallery-grid">
        <div class="gallery-empty">No images yet</div>
      </div>

      <div class="folder-controls">
        <input type="text" id="new-folder-input" class="inline-input" placeholder="New folder…">
        <button class="small-btn" id="create-folder-btn">+ Folder</button>
      </div>
    </aside>

  </div>
  <script src="/static/js/app.js"></script>
</body>
</html>
EOF_HTML

cat > $BASE/static/css/app.css << 'EOF_CSS'
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #fff8f0;
  --panel:     #ffffff;
  --card:      #f5f0ff;
  --border:    #e0d0ff;
  --pink:      #ff3b7a;
  --blue:      #1a90ff;
  --lime:      #22dd88;
  --orange:    #ff8c00;
  --purple:    #8b2be2;
  --yellow:    #ffe000;
  --text:      #1a0a2e;
  --muted:     #8870aa;
  --danger:    #ff3b3b;
  --mono:      'Space Mono', monospace;
  --sans:      'Space Grotesk', sans-serif;
  --sidebar-w: 290px;
  --gallery-w: 310px;
}

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
}

/* ── Layout ── */
.app {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr var(--gallery-w);
  grid-template-rows: auto 1fr;
  height: 100vh;
  overflow: hidden;
}

/* ── Banner header ── */
.banner-header {
  grid-column: 1 / -1;
  background: #fff0f5;
  border-bottom: 3px solid var(--pink);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  height: 90px;
  flex-shrink: 0;
}
.banner-header img {
  height: 100%;
  width: 100%;
  object-fit: cover;
  object-position: center top;
}

/* ── Sidebar ── */
.sidebar {
  background: var(--panel);
  border-right: 3px solid var(--purple);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.2s ease;
}
.sidebar.collapsed { width: 42px; }
.sidebar.collapsed .sidebar-body,
.sidebar.collapsed .sidebar-logo { display: none; }

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  background: var(--purple);
  flex-shrink: 0;
}
.sidebar-logo {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: #fff;
  text-transform: uppercase;
}

.sidebar-body {
  padding: 14px;
  overflow-y: auto;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ── Fields ── */
.field { display: flex; flex-direction: column; gap: 5px; }
.field.row { flex-direction: row; gap: 10px; }
.half { flex: 1; display: flex; flex-direction: column; gap: 5px; }

label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  color: var(--purple);
  text-transform: uppercase;
}
.muted { color: var(--muted); font-weight: 400; text-transform: none; font-size: 10px; }

textarea, input[type="text"], input[type="number"], select {
  background: var(--card);
  border: 2px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: 13px;
  padding: 7px 10px;
  border-radius: 8px;
  width: 100%;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
textarea:focus, input:focus, select:focus {
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(26,144,255,0.15);
}
textarea { resize: vertical; min-height: 85px; }

/* ── Preset grid ── */
.preset-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; }
.preset-btn {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--muted);
  font-family: var(--sans);
  font-size: 10px;
  font-weight: 600;
  padding: 5px 2px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.12s;
  text-align: center;
}
.preset-btn:hover { border-color: var(--pink); color: var(--pink); transform: translateY(-1px); }
.preset-btn.active { border-color: var(--pink); color: #fff; background: var(--pink); }

/* ── Seed row ── */
.seed-row { display: flex; gap: 6px; }
.seed-row input { flex: 1; }

/* ── Upload zone ── */
.upload-zone {
  border: 2px dashed var(--border);
  border-radius: 8px;
  position: relative;
  min-height: 70px;
  cursor: pointer;
  transition: all 0.15s;
  overflow: hidden;
  background: var(--card);
}
.upload-zone:hover, .upload-zone.drag {
  border-color: var(--blue);
  background: #eef6ff;
}
.upload-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 14px;
  color: var(--muted);
  font-size: 12px;
}
.upload-icon { font-size: 22px; color: var(--blue); }
.ref-preview {
  width: 100%;
  height: 110px;
  object-fit: cover;
  display: block;
}
.remove-ref {
  position: absolute;
  top: 5px; right: 5px;
  background: var(--pink);
  border: none;
  color: #fff;
  border-radius: 50%;
  width: 22px; height: 22px;
  font-size: 11px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  font-weight: 700;
}

/* ── Generate button ── */
.generate-btn {
  background: linear-gradient(135deg, var(--pink), var(--purple));
  color: #fff;
  border: none;
  font-family: var(--sans);
  font-size: 14px;
  font-weight: 700;
  padding: 12px;
  border-radius: 10px;
  cursor: pointer;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  transition: all 0.15s;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  box-shadow: 0 4px 14px rgba(139,43,226,0.3);
}
.generate-btn:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(139,43,226,0.45);
}
.generate-btn:disabled {
  background: var(--border);
  color: var(--muted);
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}
.spinner { animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Token meter ── */
.token-meter {
  background: #1a0a2e;
  border: 2px solid var(--purple);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.token-row { display: flex; justify-content: space-between; align-items: center; }
.token-label {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 0.12em;
}
.token-val {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 700;
}
.token-val.in-val   { color: var(--blue); }
.token-val.out-val  { color: var(--lime); }
.token-val.tot-val  { color: var(--yellow); }

/* ── Error ── */
.error-box {
  background: #fff0f0;
  border: 2px solid var(--danger);
  color: #cc0000;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.5;
}

/* ── Canvas area ── */
.canvas-area {
  display: flex;
  align-items: stretch;
  justify-content: center;
  background: var(--bg);
  overflow: hidden;
  padding: 20px;
  /* diagonal stripe bg — playful */
  background-image: repeating-linear-gradient(
    45deg,
    transparent,
    transparent 28px,
    rgba(139,43,226,0.04) 28px,
    rgba(139,43,226,0.04) 30px
  );
}
.image-frame {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  max-width: 900px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  color: var(--muted);
  text-align: center;
}
.empty-diamonds {
  display: flex;
  gap: 16px;
}
.empty-diamond {
  width: 32px; height: 32px;
  transform: rotate(45deg);
  border-radius: 4px;
}
.empty-state p { font-size: 13px; color: var(--muted); }

.result-img {
  max-width: 100%;
  max-height: calc(100vh - 240px);
  object-fit: contain;
  border-radius: 8px;
  box-shadow:
    0 0 0 3px var(--pink),
    0 0 0 6px var(--blue),
    0 12px 40px rgba(0,0,0,0.15);
}

/* ── Image meta strip ── */
.img-meta {
  width: 100%;
  max-width: 700px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.meta-tags { display: flex; flex-wrap: wrap; gap: 6px; min-height: 24px; }
.tag-chip {
  background: var(--purple);
  color: #fff;
  font-size: 11px;
  font-family: var(--mono);
  padding: 3px 9px;
  border-radius: 20px;
  display: flex;
  align-items: center;
  gap: 5px;
}
.tag-chip button {
  background: none;
  border: none;
  color: rgba(255,255,255,0.6);
  cursor: pointer;
  font-size: 10px;
  padding: 0;
  line-height: 1;
}
.tag-chip button:hover { color: var(--yellow); }

.meta-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.inline-input {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  padding: 5px 8px;
  border-radius: 6px;
  outline: none;
  transition: border-color 0.15s;
}
.inline-input:focus { border-color: var(--blue); }

/* ── Buttons ── */
.small-btn {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  font-weight: 600;
  padding: 5px 10px;
  border-radius: 6px;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  transition: all 0.12s;
  white-space: nowrap;
}
.small-btn:hover { border-color: var(--blue); color: var(--blue); transform: translateY(-1px); }
.small-btn.danger:hover { border-color: var(--danger); color: var(--danger); }

.icon-btn {
  background: rgba(255,255,255,0.2);
  border: none;
  color: #fff;
  cursor: pointer;
  font-size: 18px;
  padding: 2px 7px;
  border-radius: 4px;
  transition: background 0.15s;
  flex-shrink: 0;
  line-height: 1.4;
}
.icon-btn:hover { background: rgba(255,255,255,0.35); }
.icon-btn.dark {
  background: var(--card);
  color: var(--muted);
}
.icon-btn.dark:hover { background: var(--border); color: var(--text); }

/* ── Gallery panel ── */
.gallery-panel {
  background: var(--panel);
  border-left: 3px solid var(--blue);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.2s ease;
}
.gallery-panel.collapsed { width: 42px; }
.gallery-panel.collapsed .gallery-grid,
.gallery-panel.collapsed .folder-bar,
.gallery-panel.collapsed .tag-bar,
.gallery-panel.collapsed .folder-controls,
.gallery-panel.collapsed .search-input { display: none; }

.gallery-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  background: var(--blue);
  flex-shrink: 0;
}
.search-input {
  flex: 1;
  background: rgba(255,255,255,0.9);
  border: 2px solid transparent;
  color: var(--text);
  font-family: var(--sans);
  font-size: 12px;
  padding: 5px 10px;
  border-radius: 6px;
  outline: none;
}
.search-input:focus { border-color: #fff; }

/* ── Folder bar ── */
.folder-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 2px solid var(--border);
  flex-shrink: 0;
  background: #f0f8ff;
}
.folder-chip {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--muted);
  font-family: var(--sans);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.12s;
  display: flex;
  align-items: center;
  gap: 4px;
}
.folder-chip:hover { border-color: var(--blue); color: var(--blue); }
.folder-chip.active { border-color: var(--blue); color: #fff; background: var(--blue); }
.folder-chip .del-folder {
  font-size: 10px;
  background: none;
  border: none;
  cursor: pointer;
  color: rgba(255,255,255,0.6);
  padding: 0;
  line-height: 1;
}
.folder-chip:not(.active) .del-folder { color: var(--muted); }
.folder-chip .del-folder:hover { color: var(--danger); }

/* ── Tag bar ── */
.tag-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 6px 10px;
  border-bottom: 2px solid var(--border);
  flex-shrink: 0;
  min-height: 34px;
  background: #fdf5ff;
}
.tag-filter {
  background: #fff;
  border: 2px solid var(--border);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 20px;
  cursor: pointer;
  transition: all 0.12s;
  font-weight: 700;
}
.tag-filter:hover { border-color: var(--purple); color: var(--purple); }
.tag-filter.active { border-color: var(--purple); color: #fff; background: var(--purple); }

/* ── Gallery grid ── */
.gallery-grid {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  align-content: start;
}
.gallery-empty {
  color: var(--muted);
  font-size: 12px;
  padding: 20px;
  grid-column: 1/-1;
  text-align: center;
}

.gallery-thumb {
  aspect-ratio: 1;
  border-radius: 6px;
  overflow: hidden;
  cursor: pointer;
  border: 3px solid transparent;
  transition: all 0.15s;
  position: relative;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
.gallery-thumb:hover { border-color: var(--pink); transform: scale(1.03); }
.gallery-thumb.active { border-color: var(--pink); box-shadow: 0 0 0 2px var(--purple); }
.gallery-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.gallery-thumb .thumb-prompt {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  background: linear-gradient(transparent, rgba(26,10,46,0.9));
  color: #fff;
  font-size: 9px;
  padding: 16px 5px 4px;
  opacity: 0;
  transition: opacity 0.15s;
  line-height: 1.3;
}
.gallery-thumb:hover .thumb-prompt { opacity: 1; }

/* SVG badge */
.svg-badge {
  position: absolute;
  top: 5px; right: 5px;
  background: var(--lime);
  color: #1a0a2e;
  font-family: var(--mono);
  font-size: 8px;
  font-weight: 700;
  padding: 2px 5px;
  border-radius: 3px;
  opacity: 0;
  transition: opacity 0.15s;
}
.gallery-thumb:hover .svg-badge { opacity: 1; }

/* ── Folder controls ── */
.folder-controls {
  display: flex;
  gap: 6px;
  padding: 10px;
  border-top: 2px solid var(--border);
  flex-shrink: 0;
  background: #f0f8ff;
}
.folder-controls .inline-input { flex: 1; }

/* ── Utilities ── */
.hidden { display: none !important; }
EOF_CSS

cat > $BASE/static/js/app.js << 'EOF_JS'
/* ── State ── */
let activeImage  = null;   // current image object
let activeFolder = "";
let activeTag    = "";
let searchQ      = "";
let refFile      = null;
let selectedPreset = "";

/* ── DOM refs ── */
const $ = id => document.getElementById(id);
const prompt       = $("prompt");
const widthInput   = $("width");
const heightInput  = $("height");
const seedInput    = $("seed");
const generateBtn  = $("generate-btn");
const btnLabel     = $("btn-label");
const spinner      = $("spinner");
const tokenMeter   = $("token-meter");
const tIn          = $("t-in");
const tOut         = $("t-out");
const tTotal       = $("t-total");
const errorBox     = $("error-box");
const resultImg    = $("result-img");
const emptyState   = $("empty-state");
const imgMeta      = $("img-meta");
const metaTags     = $("meta-tags");
const tagInput     = $("tag-input");
const addTagBtn    = $("add-tag-btn");
const folderSelect = $("folder-select");
const downloadBtn  = $("download-btn");
const deleteBtn    = $("delete-btn");
const galleryGrid  = $("gallery-grid");
const searchInput  = $("search-input");
const folderBar    = $("folder-bar");
const tagBar       = $("tag-bar");
const uploadZone   = $("upload-zone");
const uploadInner  = $("upload-inner");
const refInput     = $("ref-input");
const refPreview   = $("ref-preview");
const removeRef    = $("remove-ref");
const newFolderInput = $("new-folder-input");
const createFolderBtn = $("create-folder-btn");

/* ── Preset picker ── */
document.querySelectorAll(".preset-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".preset-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    selectedPreset = btn.dataset.preset;
  });
});
document.querySelector('.preset-btn[data-preset=""]').classList.add("active");

/* ── Seed random ── */
$("random-seed").addEventListener("click", () => {
  seedInput.value = Math.floor(Math.random() * 99999);
});

/* ── Sidebar / gallery collapse ── */
$("toggle-sidebar").addEventListener("click", () => {
  const s = $("sidebar");
  s.classList.toggle("collapsed");
  $("toggle-sidebar").textContent = s.classList.contains("collapsed") ? "›" : "‹";
});
$("toggle-gallery").addEventListener("click", () => {
  const g = $("gallery-panel");
  g.classList.toggle("collapsed");
  $("toggle-gallery").textContent = g.classList.contains("collapsed") ? "‹" : "›";
});

/* ── Upload zone ── */
uploadZone.addEventListener("click", () => !refFile && refInput.click());
uploadZone.addEventListener("dragover", e => { e.preventDefault(); uploadZone.classList.add("drag"); });
uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("drag"));
uploadZone.addEventListener("drop", e => {
  e.preventDefault();
  uploadZone.classList.remove("drag");
  const f = e.dataTransfer.files[0];
  if (f && f.type.startsWith("image/")) setRef(f);
});
refInput.addEventListener("change", () => refInput.files[0] && setRef(refInput.files[0]));
removeRef.addEventListener("click", e => { e.stopPropagation(); clearRef(); });

function setRef(file) {
  refFile = file;
  const url = URL.createObjectURL(file);
  refPreview.src = url;
  refPreview.classList.remove("hidden");
  uploadInner.classList.add("hidden");
  removeRef.classList.remove("hidden");
}
function clearRef() {
  refFile = null;
  refPreview.classList.add("hidden");
  refPreview.src = "";
  uploadInner.classList.remove("hidden");
  removeRef.classList.add("hidden");
  refInput.value = "";
}

/* ── Generate ── */
generateBtn.addEventListener("click", generate);
prompt.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate();
});

async function generate() {
  const p = prompt.value.trim();
  if (!p) { showError("Enter a prompt."); return; }

  setLoading(true);
  hideError();

  const fd = new FormData();
  fd.append("prompt", p);
  fd.append("preset", selectedPreset);
  fd.append("seed",   seedInput.value);
  fd.append("width",  widthInput.value);
  fd.append("height", heightInput.value);
  if (refFile) fd.append("ref", refFile);

  try {
    const res  = await fetch("/api/generate", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Generation failed");

    showImage(data);
    setTokens(data.tokens_in, data.tokens_out);
    await loadGallery();
    await loadFolders();
    selectThumb(data.id);
  } catch (err) {
    showError(err.message);
  } finally {
    setLoading(false);
  }
}

/* ── Display image ── */
function showImage(img) {
  activeImage = img;
  resultImg.src = img.url + "?t=" + Date.now();
  resultImg.classList.remove("hidden");
  emptyState.classList.add("hidden");
  imgMeta.classList.remove("hidden");

  downloadBtn.href     = img.url;
  downloadBtn.download = img.filename || "poly.png";

  const svgBtn = $("svg-download-btn");
  if (img.svg_url) {
    svgBtn.href     = img.svg_url;
    svgBtn.download = (img.filename || "poly").replace(".png", ".svg");
    svgBtn.classList.remove("hidden");
  } else {
    svgBtn.classList.add("hidden");
  }

  renderMetaTags(img.tags || []);
  populateFolderSelect(img.folders || []);
}

function renderMetaTags(tags) {
  metaTags.innerHTML = tags.map(t => `
    <span class="tag-chip">
      ${t}
      <button onclick="removeTag('${t}')" title="Remove tag">✕</button>
    </span>
  `).join("");
}

async function populateFolderSelect(current) {
  const res  = await fetch("/api/folders");
  const all  = await res.json();
  folderSelect.innerHTML = '<option value="">Add to folder…</option>';
  all.forEach(f => {
    if (!current.includes(f.name)) {
      const o = document.createElement("option");
      o.value = o.textContent = f.name;
      folderSelect.appendChild(o);
    }
  });
}

/* ── Tokens ── */
function setTokens(inp, out) {
  tIn.textContent    = inp.toLocaleString();
  tOut.textContent   = out.toLocaleString();
  tTotal.textContent = (inp + out).toLocaleString();
  tokenMeter.classList.remove("hidden");
}

/* ── Tags ── */
addTagBtn.addEventListener("click", async () => {
  if (!activeImage) return;
  const name = tagInput.value.trim().toLowerCase();
  if (!name) return;
  await fetch(`/api/images/${activeImage.id}/tags`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  tagInput.value = "";
  activeImage.tags = [...(activeImage.tags || []), name];
  renderMetaTags(activeImage.tags);
  await loadTagBar();
});
tagInput.addEventListener("keydown", e => e.key === "Enter" && addTagBtn.click());

async function removeTag(name) {
  if (!activeImage) return;
  await fetch(`/api/images/${activeImage.id}/tags/${name}`, { method: "DELETE" });
  activeImage.tags = (activeImage.tags || []).filter(t => t !== name);
  renderMetaTags(activeImage.tags);
  await loadTagBar();
}

/* ── Folders ── */
folderSelect.addEventListener("change", async () => {
  if (!activeImage || !folderSelect.value) return;
  const name = folderSelect.value;
  await fetch(`/api/images/${activeImage.id}/folders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  activeImage.folders = [...(activeImage.folders || []), name];
  populateFolderSelect(activeImage.folders);
  await loadFolders();
});

createFolderBtn.addEventListener("click", async () => {
  const name = newFolderInput.value.trim();
  if (!name) return;
  await fetch("/api/folders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  newFolderInput.value = "";
  await loadFolders();
});

/* ── Delete ── */
deleteBtn.addEventListener("click", async () => {
  if (!activeImage || !confirm("Delete this image?")) return;
  await fetch(`/api/images/${activeImage.id}`, { method: "DELETE" });
  activeImage = null;
  resultImg.classList.add("hidden");
  emptyState.classList.remove("hidden");
  imgMeta.classList.add("hidden");
  tokenMeter.classList.add("hidden");
  await loadGallery();
  await loadFolders();
});

/* ── Gallery ── */
async function loadGallery() {
  const params = new URLSearchParams();
  if (searchQ)      params.set("q",      searchQ);
  if (activeFolder) params.set("folder", activeFolder);
  if (activeTag)    params.set("tag",    activeTag);

  const res    = await fetch("/api/images?" + params);
  const images = await res.json();

  if (!images.length) {
    galleryGrid.innerHTML = '<div class="gallery-empty">No images yet</div>';
    return;
  }

  galleryGrid.innerHTML = images.map(img => `
    <div class="gallery-thumb ${activeImage?.id === img.id ? 'active' : ''}"
         data-id="${img.id}" onclick="loadImage(${JSON.stringify(JSON.stringify(img))})">
      <img src="${img.thumb_url}" alt="" loading="lazy">
      <div class="thumb-prompt">${img.prompt || ''}</div>
    </div>
  `).join("");

  await loadTagBar();
}

function loadImage(jsonStr) {
  const img = JSON.parse(jsonStr);
  showImage(img);
  setTokens(img.tokens_in || 0, img.tokens_out || 0);
  selectThumb(img.id);
}

function selectThumb(id) {
  document.querySelectorAll(".gallery-thumb").forEach(t => {
    t.classList.toggle("active", t.dataset.id === id);
  });
}

/* ── Folders sidebar ── */
async function loadFolders() {
  const res     = await fetch("/api/folders");
  const folders = await res.json();

  // rebuild folder bar (keep "All")
  const existing = folderBar.querySelector('[data-folder=""]');
  folderBar.innerHTML = "";
  folderBar.appendChild(existing);

  folders.forEach(f => {
    const chip = document.createElement("button");
    chip.className = "folder-chip" + (activeFolder === f.name ? " active" : "");
    chip.dataset.folder = f.name;
    chip.innerHTML = `${f.name} <span style="color:var(--muted);font-size:10px">${f.count}</span>
      <button class="del-folder" onclick="deleteFolder(event,'${f.name}')">✕</button>`;
    chip.addEventListener("click", () => setFolder(f.name));
    folderBar.appendChild(chip);
  });
}

function setFolder(name) {
  activeFolder = name;
  document.querySelectorAll(".folder-chip").forEach(c => {
    c.classList.toggle("active", c.dataset.folder === name);
  });
  loadGallery();
}

async function deleteFolder(e, name) {
  e.stopPropagation();
  if (!confirm(`Delete folder "${name}"?`)) return;
  await fetch(`/api/folders/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (activeFolder === name) activeFolder = "";
  await loadFolders();
  await loadGallery();
}

/* ── Tag bar ── */
async function loadTagBar() {
  const res  = await fetch("/api/tags");
  const tags = await res.json();
  tagBar.innerHTML = tags.map(t => `
    <button class="tag-filter ${activeTag === t.name ? 'active' : ''}"
            onclick="setTag('${t.name}')">
      ${t.name} <span style="opacity:0.5">${t.count}</span>
    </button>
  `).join("");
}

function setTag(name) {
  activeTag = activeTag === name ? "" : name;
  loadTagBar();
  loadGallery();
}

/* ── Search ── */
let searchTimer;
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    searchQ = searchInput.value.trim();
    loadGallery();
  }, 300);
});

/* ── Utils ── */
function setLoading(on) {
  generateBtn.disabled = on;
  spinner.classList.toggle("hidden", !on);
  btnLabel.textContent = on ? "Generating…" : "Generate";
}
function showError(msg) { errorBox.textContent = msg; errorBox.classList.remove("hidden"); }
function hideError()    { errorBox.classList.add("hidden"); }

/* ── Init ── */
loadGallery();
loadFolders();
EOF_JS

echo 'All files written'
