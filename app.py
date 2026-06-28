import os, json, sqlite3, uuid, base64, sys
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template, g, after_this_request
from PIL import Image as PILImage
import anthropic
import anti_abuse

# ── paths ──────────────────────────────────────────────────────────────────
# RENDERS_DIR/UPLOADS_DIR stay under static/ (Flask serves them at /static/...);
# in a hardened container, mount writable volumes at those paths. The DB isn't
# served, so POLY_DB_PATH is env-overridable to a writable volume — this is what
# lets the app run on a read-only root filesystem. Defaults preserve the local layout.
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RENDERS_DIR = os.path.join(BASE_DIR, "static", "renders")
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads")
DB_PATH     = os.environ.get("POLY_DB_PATH") or os.path.join(BASE_DIR, "poly.db")
RENDERER    = os.path.join(BASE_DIR, "renderer.py")

os.makedirs(RENDERS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

# inject renderer into path
sys.path.insert(0, BASE_DIR)
from renderer import render as poly_render, validate_scene as renderer_validate, SceneValidationError
import sandbox

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB upload limit

# anti-abuse setup: pins PIL.MAX_IMAGE_PIXELS and syncs MAX_CONTENT_LENGTH to
# MAX_UPLOAD_MB. This is the module's only side-effecting call.
anti_abuse.init(app)

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

def call_claude(prompt, ref_path=None, preset=None, seed=42, model='claude-sonnet-4-6', ref_mime=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_content = []

    if ref_path and os.path.exists(ref_path):
        # Use the MIME detected by validate_image_upload (the real content type),
        # falling back to extension only if a caller didn't supply one.
        if ref_mime:
            mime = ref_mime
        else:
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
    # Issue a signed, timestamped form token embedded in the page. The generate
    # endpoint rejects submissions with a missing/forged/too-fast token.
    # CAPTCHA_SITEKEY drives the client-side Turnstile widget; it must be set
    # alongside CAPTCHA_SECRET (server) for CAPTCHA to work in a browser.
    return render_template("index.html",
                           form_token=anti_abuse.make_form_token(),
                           captcha_sitekey=os.environ.get("CAPTCHA_SITEKEY", ""))

# ── routes: generation ─────────────────────────────────────────────────────
@app.route("/api/generate", methods=["POST"])
@anti_abuse.generate_rate_limiters()   # strict per-min + per-day cap, applied first
def generate():
    prompt  = request.form.get("prompt", "").strip()
    preset  = request.form.get("preset") or None
    # Untrusted numerics: parse + clamp so `seed=abc` no longer 500s and
    # `width=999999` can't DoS the renderer.
    seed    = anti_abuse.safe_int(request.form.get("seed", 42), 42,
                                  anti_abuse.SEED_MIN, anti_abuse.SEED_MAX)
    width   = anti_abuse.safe_int(request.form.get("width", 1024), 1024,
                                  anti_abuse.DIM_MIN, anti_abuse.DIM_MAX)
    height  = anti_abuse.safe_int(request.form.get("height", 1024), 1024,
                                  anti_abuse.DIM_MIN, anti_abuse.DIM_MAX)

    if not prompt:
        return jsonify({"error": "Prompt required"}), 400

    # ── spam / bot gates: run BEFORE any Claude spend (fail fast) ──
    if anti_abuse.check_honeypot(request.form):
        # Bot filled the hidden field. Reject quietly.
        return jsonify({"error": "Submission rejected."}), 400
    if not (request.form.get("ack") or "").strip():
        return jsonify({"error": "You must accept the content policy to generate."}), 400
    ok, why = anti_abuse.verify_form_token(request.form.get("form_token", ""))
    if not ok:
        return jsonify({"error": f"Submission rejected ({why})."}), 400
    if not anti_abuse.verify_captcha(request.form.get("captcha_token", "")):
        return jsonify({"error": "CAPTCHA verification failed."}), 400

    # ── prompt moderation (keyword always; model pass if MODERATION_ENABLED) ──
    # The content policy prohibits NSFW *and* illegal content, so any non-"allow"
    # verdict (both "block" and the softer "flag"=NSFW) is rejected.
    mod_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
    verdict = anti_abuse.moderate_prompt(prompt, mod_client)
    if verdict["action"] != "allow":
        return jsonify({"error": f"Prompt rejected: {verdict['reason']}"}), 400

    # ── handle optional ref image: validate + sanitize, never trust the bytes ──
    ref_path = None
    ref_pil  = None
    ref_mime = None
    if "ref" in request.files and request.files["ref"].filename:
        try:
            clean = anti_abuse.validate_image_upload(request.files["ref"])
        except anti_abuse.UploadRejected as e:
            return jsonify({"error": e.message}), e.status
        img_verdict = anti_abuse.moderate_image(clean.image, mod_client)
        if img_verdict["action"] != "allow":
            return jsonify({"error": f"Reference image rejected: {img_verdict['reason']}"}), 400
        ref_name = f"{uuid.uuid4().hex}.{clean.ext}"
        ref_path = os.path.join(UPLOADS_DIR, ref_name)
        clean.save(ref_path)          # re-encoded, EXIF/polyglot stripped
        ref_pil  = clean.image
        ref_mime = clean.mime
        # Delete the ref after the request finishes (no TTL sweep needed):
        # uploads/ is otherwise write-only and never pruned.
        @after_this_request
        def _drop_ref(response, _p=ref_path):
            try:
                if os.path.exists(_p):
                    os.remove(_p)
            except OSError:
                pass
            return response

    model = request.form.get('model', 'claude-sonnet-4-6')
    try:
        scene_code, tokens_in, tokens_out = call_claude(prompt, ref_path, preset, seed, model, ref_mime)
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

    # sandbox safety check — reject scene code that uses forbidden constructs
    # (imports, dunder access, os/eval/exec, etc.). Retry once asking Claude to
    # remove them, since an occasional model slip shouldn't fail a legit prompt.
    safety_err = renderer_validate(scene_code)
    if safety_err:
        try:
            fix_prompt = (f"The following Python scene code was rejected by a security sandbox "
                          f"because: {safety_err}. Rewrite it to draw the same scene using ONLY "
                          f"the pre-injected names (img, draw, W, H, rng, ref, palette, Image, "
                          f"ImageDraw, ImageFont, math, random) with no imports, no dunder "
                          f"attributes, and no eval/exec/open. Return only the corrected code:\n\n{scene_code}")
            scene_code, tokens_in3, tokens_out3 = call_claude(fix_prompt, None, preset, seed, model)
            tokens_in  += tokens_in3
            tokens_out += tokens_out3
        except Exception:
            return jsonify({"error": f"Scene rejected by sandbox: {safety_err}"}), 400
        safety_err2 = renderer_validate(scene_code)
        if safety_err2:
            return jsonify({"error": f"Scene rejected by sandbox: {safety_err2}", "scene_code": scene_code}), 400

    # render — executed in a resource-limited subprocess sandbox (sandbox.py)
    img_id   = uuid.uuid4().hex
    filename = f"{img_id}.png"

    try:
        result = sandbox.run_scene(
            scene_code, filename=filename,
            width=width, height=height, seed=seed,
            ref=ref_pil, preset=preset, thumbnail=True,
            _output_dir=RENDERS_DIR
        )
    except SceneValidationError as e:
        return jsonify({"error": f"Scene rejected by sandbox: {e}", "scene_code": scene_code}), 400
    except sandbox.RenderTimeout as e:
        return jsonify({"error": f"Render timed out: {e}"}), 400
    except sandbox.RenderError as e:
        return jsonify({"error": f"Render error: {e}", "scene_code": scene_code}), 500
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
@anti_abuse.mutate_rate_limit
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
@anti_abuse.mutate_rate_limit
def add_tag(img_id):
    name = (request.get_json(silent=True) or {}).get("name", "").strip().lower()
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
@anti_abuse.mutate_rate_limit
def remove_tag(img_id, tag_name):
    db     = get_db()
    tag    = db.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()
    if tag:
        db.execute("DELETE FROM image_tags WHERE image_id=? AND tag_id=?", (img_id, tag["id"]))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/images/<img_id>/folders", methods=["POST"])
@anti_abuse.mutate_rate_limit
def add_to_folder(img_id):
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
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
@anti_abuse.mutate_rate_limit
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
@anti_abuse.mutate_rate_limit
def create_folder():
    name = (request.get_json(silent=True) or {}).get("name", "").strip()
    if not name: return jsonify({"error": "Name required"}), 400
    db = get_db()
    existing = db.execute("SELECT id FROM folders WHERE name=?", (name,)).fetchone()
    if existing: return jsonify({"error": "Folder exists"}), 409
    fid = uuid.uuid4().hex
    db.execute("INSERT INTO folders (id,name) VALUES (?,?)", (fid, name))
    db.commit()
    return jsonify({"ok": True, "id": fid, "name": name})

@app.route("/api/folders/<name>", methods=["DELETE"])
@anti_abuse.mutate_rate_limit
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
