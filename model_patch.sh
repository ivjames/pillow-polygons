#!/bin/bash
set -e
BASE=/var/www/poly

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
          <label>Model</label>
          <select id="model-select">
            <option value="claude-haiku-4-5-20251001">Haiku — fast &amp; cheap</option>
            <option value="claude-sonnet-4-6" selected>Sonnet — default</option>
            <option value="claude-opus-4-6">Opus — best</option>
          </select>
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
  fd.append("model",  $("model-select").value);
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

pm2 restart poly-app
echo 'Done'
