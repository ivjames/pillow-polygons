#!/bin/bash
set -e
BASE=/var/www/poly

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
    _base_palette = {"bg": (20,20,30), "atmosphere": (30,30,50,40), "accent": (200,200,255), "grain": 2000}
    palette   = {**_base_palette, **(PRESETS.get(preset, {}) if preset else {})}

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
Always use palette.get('bg', (20,20,30)) style access — palette may be empty if no preset selected.
Available fonts (use try/except):
  /usr/share/fonts/truetype/google-fonts/Poppins-Light.ttf
  /usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf
  /usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf

Output raw Python only. Nothing else."""

def image_to_b64(path):
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")

def call_claude(prompt, ref_path=None, preset=None, seed=42, model='claude-sonnet-4-6'):
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
        model=model,
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

    model = request.form.get('model', 'claude-sonnet-4-6')
    try:
        scene_code, tokens_in, tokens_out = call_claude(prompt, ref_path, preset, seed, model)
    except Exception as e:
        return jsonify({"error": f"Claude API error: {e}"}), 500

    # syntax check — retry once if bad
    def check_syntax(code):
        try:
            compile(code, "<scene>", "exec")
            return None
        except SyntaxError as e:
            return str(e)

    syntax_err = check_syntax(scene_code)
    if syntax_err:
        try:
            fix_prompt = f"The following Python scene code has a syntax error: {syntax_err}\n\nFix it and return only the corrected code, no explanation:\n\n{scene_code}"
            scene_code, tokens_in2, tokens_out2 = call_claude(fix_prompt, None, preset, seed, model)
            tokens_in  += tokens_in2
            tokens_out += tokens_out2
        except Exception as e:
            return jsonify({"error": f"Syntax error and fix failed: {syntax_err}"}), 500
        syntax_err2 = check_syntax(scene_code)
        if syntax_err2:
            return jsonify({"error": f"Syntax error persisted after retry: {syntax_err2}", "scene_code": scene_code}), 500

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

pm2 restart poly-app
echo 'Done'
