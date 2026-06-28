import os, json, sqlite3, uuid, sys
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g
import anthropic
import anti_abuse

# ── paths ──────────────────────────────────────────────────────────────────
# RENDERS_DIR stays under static/ (Flask serves it at /static/renders/...);
# in a hardened container, mount a writable volume there. The DB isn't served,
# so POLY_DB_PATH is env-overridable to a writable volume — this is what lets
# the app run on a read-only root filesystem. Defaults preserve the local layout.
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RENDERS_DIR = os.path.join(BASE_DIR, "static", "renders")
DB_PATH     = os.environ.get("POLY_DB_PATH") or os.path.join(BASE_DIR, "poly.db")
RENDERER    = os.path.join(BASE_DIR, "renderer.py")

os.makedirs(RENDERS_DIR, exist_ok=True)

# inject renderer into path
sys.path.insert(0, BASE_DIR)
from renderer import render as poly_render, validate_scene as renderer_validate, SceneValidationError
import sandbox
import jobqueue
import scene_json

app = Flask(__name__)
# Text-only API (no uploads): cap request bodies at 1MB — generate only carries a
# prompt + a few small form fields, so anything larger is junk/abuse.
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

# anti-abuse setup: pins PIL.MAX_IMAGE_PIXELS (decompression-bomb ceiling for the
# renderer). This is the module's only side-effecting call.
anti_abuse.init(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── token cost accounting ────────────────────────────────────────────────────
# Per-model Anthropic list price in USD per 1M tokens, (input, output). Keyed by
# model-id prefix so a minor version bump (sonnet-4-6 -> 4-7) keeps its pricing
# without a code change. Unknown models fall back to the Sonnet tier. Update if
# Anthropic changes pricing; see https://docs.anthropic.com/en/docs/about-claude/pricing
PRICING = {
    "claude-opus":   (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku":  (1.0, 5.0),
}
_DEFAULT_PRICING = (3.0, 15.0)


def compute_cost(model, tokens_in, tokens_out):
    """USD cost for one generation, from the stored model + token counts. Never
    raises (cost reporting must not break a response); unknown models price at the
    Sonnet tier."""
    rate_in, rate_out = _DEFAULT_PRICING
    for prefix, rates in PRICING.items():
        if model and str(model).startswith(prefix):
            rate_in, rate_out = rates
            break
    cost = (tokens_in or 0) / 1_000_000 * rate_in + (tokens_out or 0) / 1_000_000 * rate_out
    return round(cost, 6)

# Where scene code actually executes:
#   "local"  — in a subprocess sandbox inside this process (default; simple dev).
#   "worker" — handed to the network-less render worker over the shared job queue,
#              so this web tier never exec()s scene code (issue #2). See render_worker.py.
RENDER_MODE = os.environ.get("RENDER_MODE", "local").strip().lower()

# What the model is asked to produce, and which renderer path runs it:
#   "python" — raw Pillow scene code, exec'd by renderer.render(). Expressive
#              (Turing-complete) but a prompt-injection -> RCE surface, contained
#              by the sandbox stack (AST allowlist + subprocess + network-less worker).
#   "json"   — a declarative JSON scene drawn by renderer.render_json() over a
#              fixed primitive set. No exec, so there is no RCE surface at all —
#              the threat model shrinks to data validation. See scene_json.py.
SCENE_FORMAT = os.environ.get("SCENE_FORMAT", "python").strip().lower()


def _render_scene(scene_payload, *, filename, width, height, seed, preset):
    """Dispatch a render to the worker queue or the in-process sandbox. Both
    return the renderer's dict and raise the same exceptions. The scene format
    (python/json) is taken from SCENE_FORMAT and selects the renderer path."""
    if RENDER_MODE == "worker":
        return jobqueue.submit_and_wait(
            scene_payload, filename=filename, width=width, height=height, seed=seed,
            preset=preset, thumbnail=True, output_dir=RENDERS_DIR, scene_format=SCENE_FORMAT)
    return sandbox.run_scene(
        scene_payload, filename=filename, width=width, height=height, seed=seed,
        preset=preset, thumbnail=True, _output_dir=RENDERS_DIR, scene_format=SCENE_FORMAT)

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
            model       TEXT,
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
        # Auto-migrate DBs created before the `model` column existed, so cost
        # reporting works without a manual migration step.
        cols = {r[1] for r in db.execute("PRAGMA table_info(images)").fetchall()}
        if "model" not in cols:
            db.execute("ALTER TABLE images ADD COLUMN model TEXT")
    print("DB initialised")

init_db()

# ── helpers ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are the Pillow Polygons scene code generator.

OUTPUT FORMAT — THIS IS A HARD REQUIREMENT:
- Respond with raw Python source code and NOTHING else.
- Do NOT wrap the code in markdown fences (no ``` or ```python).
- Do NOT add any prose, explanation, comments-about-the-answer, or preamble
  before or after the code. No "Here is..." and no closing remarks.
- The FIRST character of your response must be the first character of Python
  code, and the LAST character must be the last character of Python code.
Any deviation from this format breaks the renderer.

The following are pre-injected and available without importing:
  img, draw, W, H, rng, palette,
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

Available presets inject palette dict with keys: bg, atmosphere, accent, grain
Always use palette.get('bg', (20,20,30)) style access — palette may be empty if no preset selected.
Available fonts (use try/except):
  /usr/share/fonts/truetype/google-fonts/Poppins-Light.ttf
  /usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf
  /usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf

Remember the output format: raw Python only, no markdown fences, no prose. Nothing else."""


SYSTEM_PROMPT_JSON = """You are the Pillow Polygons scene generator.

Output a single JSON object that DESCRIBES the scene to draw — and NOTHING else.
No markdown fences, no prose, no explanation. The response must be valid JSON,
its first character '{' and its last character '}'.

The canvas is W×H pixels, origin at the top-left, coordinates in pixels.

Schema:
{
  "background": {"type": "gradient", "from": [r,g,b], "to": [r,g,b], "direction": "vertical"|"horizontal"},
                 // OR {"type":"radial","inner":[r,g,b],"outer":[r,g,b],"cx":x,"cy":y,"r":n}
                 // OR {"type":"solid","color":[r,g,b]} — omit to keep the preset background
  "layers": [
    {"alpha": 0-255,            // optional overall layer opacity, for soft glows/atmosphere
     "ops": [ <op>, ... ]}
  ]
}

Each <op> is exactly one of:
  {"op":"polygon","points":[[x,y],...],"fill":<color>,"outline":<color>,"width":n}
  {"op":"ellipse","bbox":[x0,y0,x1,y1],"fill":<color>,"outline":<color>,"width":n}
  {"op":"rectangle","bbox":[x0,y0,x1,y1],"fill":<color>,"outline":<color>,"width":n}
  {"op":"line","points":[[x,y],...],"fill":<color>,"width":n}
  {"op":"arc","bbox":[x0,y0,x1,y1],"start":deg,"end":deg,"fill":<color>,"width":n}
  {"op":"bezier","points":[[x,y]×3or4],"stroke":<color>,"fill":<color>,"width":n,"closed":false}
                 // 3 points = quadratic, 4 = cubic; smooth curve. "fill" only if closed.
  {"op":"point","points":[[x,y],...],"fill":<color>}
  {"op":"text","xy":[x,y],"text":"...","fill":<color>,"size":n}
  {"op":"grain","count":n,"fill":<color>,"alpha":a}   // n random 1px speckles for texture
  {"op":"vignette","strength":0-255}                  // dark edge falloff; end scenes with this
  {"op":"scatter","count":n,"area":[x0,y0,x1,y1],"shape":<leaf op around the origin>}
                 // stamps `count` copies of shape at random spots in area (e.g. a star field)
  {"op":"repeat","nx":a,"ny":b,"dx":px,"dy":px,"x0":px,"y0":px,"shape":<leaf op around the origin>}
                 // stamps shape across an a×b grid (e.g. windows, tiles)

<color> is [r,g,b] or [r,g,b,a] (0-255), or one of the palette key strings
"bg", "atmosphere", or "accent".

BE TOKEN-EFFICIENT — this matters:
- NEVER enumerate many near-identical shapes by hand. For repetition use "scatter"
  (random) or "repeat" (grid); for smooth curves use "bezier" (3-4 points) instead
  of a long "points" list; for gradients use the gradient/radial background.
- A scatter/repeat "shape" is defined around the origin (0,0) and is translated to
  each placement — keep it small, e.g. {"op":"ellipse","bbox":[-2,-2,2,2],...}.
- Aim for a compact scene: a few dozen ops is plenty. The expanding ops do the
  heavy lifting so you write little.

Composition guidance (match the house style):
- Gradient or radial background, then build subjects from layered polygons +
  ellipses with separate shadow / base / highlight layers.
- Eyes: socket → iris → pupil → gleam. Use bezier for organic edges.
- A low-alpha "scatter" or "grain" layer for texture; finish with a "vignette".

Hard limits: ≤ 64 layers, ≤ 5000 ops, ≤ 2000 points/shape, ≤ 20000 expanded
primitives total (scatter+repeat+grain). Output raw JSON only — nothing before
'{' or after '}'."""

def _strip_code_fences(text):
    """Enforce the 'raw Python only' output contract on the model's behalf.

    The system prompt demands no markdown, but models occasionally wrap the scene
    code in a ```python ... ``` fence anyway. Rather than fail (and pay for a
    retry), recover the code: if the response is fenced, drop the fence lines and
    return the inner body. A response with no fence is returned unchanged.
    """
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    # Drop the opening fence (``` or ```python) ...
    lines = lines[1:]
    # ... and the closing fence if present.
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def call_claude(prompt, preset=None, seed=42, model='claude-sonnet-4-6', system=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    preset_note = f"\nActive preset: {preset}" if preset else ""
    seed_note   = f"\nSeed: {seed}"
    user_content = [{
        "type": "text",
        "text": f"{prompt}{preset_note}{seed_note}"
    }]

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system or SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}]
    )

    # Enforce the raw-Python output contract even if the model wrapped it in a
    # markdown fence, so a stray ``` doesn't cost a syntax-error retry.
    scene_code   = _strip_code_fences(response.content[0].text)
    tokens_in    = response.usage.input_tokens
    tokens_out   = response.usage.output_tokens

    return scene_code, tokens_in, tokens_out


class SceneGenError(Exception):
    """A scene the model couldn't produce acceptably. Carries the HTTP status and
    (optionally) the offending scene text to echo back to the client."""
    def __init__(self, message, status=400, scene=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.scene = scene


def _check_syntax(code):
    try:
        compile(code, "<scene>", "exec")
        return None
    except SyntaxError as e:
        return str(e)


def _generate_python_scene(prompt, preset, seed, model):
    """Python path: generate scene code, then gate it on a syntax check and the
    AST sandbox, each with one Claude retry (a model slip shouldn't fail a legit
    prompt). Returns (code, tokens_in, tokens_out) or raises SceneGenError."""
    try:
        code, tin, tout = call_claude(prompt, preset, seed, model)
    except Exception as e:
        raise SceneGenError(f"Claude API error: {e}", 500)

    err = _check_syntax(code)
    if err:
        try:
            code, ti, to = call_claude(
                f"The following Python scene code has a syntax error: {err}\n\nFix it and "
                f"return ONLY the corrected raw Python code — no markdown fences, no "
                f"explanation, no prose:\n\n{code}", preset, seed, model)
            tin += ti; tout += to
        except Exception:
            raise SceneGenError(f"Syntax error and fix failed: {err}", 500)
        err2 = _check_syntax(code)
        if err2:
            raise SceneGenError(f"Syntax error persisted after retry: {err2}", 500, scene=code)

    serr = renderer_validate(code)
    if serr:
        try:
            code, ti, to = call_claude(
                f"The following Python scene code was rejected by a security sandbox because: "
                f"{serr}. Rewrite it to draw the same scene using ONLY the pre-injected names "
                f"(img, draw, W, H, rng, palette, Image, ImageDraw, ImageFont, math, random) "
                f"with no imports, no dunder attributes, and no eval/exec/open. Return ONLY the "
                f"corrected raw Python code — no markdown fences, no explanation:\n\n{code}",
                preset, seed, model)
            tin += ti; tout += to
        except Exception:
            raise SceneGenError(f"Scene rejected by sandbox: {serr}", 400)
        serr2 = renderer_validate(code)
        if serr2:
            raise SceneGenError(f"Scene rejected by sandbox: {serr2}", 400, scene=code)

    return code, tin, tout


def _parse_scene_json(raw):
    """Return (scene_dict, None) or (None, error_str). Fences are tolerated."""
    try:
        scene = json.loads(_strip_code_fences(raw))
    except (ValueError, TypeError) as e:
        return None, f"not valid JSON ({e})"
    err = scene_json.validate_scene_json(scene)
    if err:
        return None, err
    return scene, None


def _generate_json_scene(prompt, preset, seed, model):
    """JSON path: generate a declarative scene, validate it against the schema,
    one retry on a parse/schema miss. There is no code to sandbox here — a valid
    scene is just data. Returns (scene_dict, tokens_in, tokens_out)."""
    try:
        raw, tin, tout = call_claude(prompt, preset, seed, model, system=SYSTEM_PROMPT_JSON)
    except Exception as e:
        raise SceneGenError(f"Claude API error: {e}", 500)

    scene, err = _parse_scene_json(raw)
    if err:
        try:
            raw, ti, to = call_claude(
                f"Your previous response was rejected: {err}. Return ONLY a single valid JSON "
                f"object matching the scene schema — no markdown fences, no prose:\n\n{raw}",
                preset, seed, model, system=SYSTEM_PROMPT_JSON)
            tin += ti; tout += to
        except Exception:
            raise SceneGenError(f"Scene rejected: {err}", 400)
        scene, err2 = _parse_scene_json(raw)
        if err2:
            raise SceneGenError(f"Scene rejected after retry: {err2}", 400, scene=raw)

    return scene, tin, tout


def _scene_text(payload):
    """Serialize a scene payload for storage / error echoes: code is already a
    string; a JSON scene dict is dumped compactly."""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload)


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
    # Derived, not stored: USD cost from the model + token counts (works for old
    # rows too — a NULL model just prices at the default tier).
    d["cost_usd"] = compute_cost(d.get("model"), d.get("tokens_in"), d.get("tokens_out"))
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

    model = request.form.get('model', 'claude-sonnet-4-6')
    try:
        if SCENE_FORMAT == "json":
            scene_payload, tokens_in, tokens_out = _generate_json_scene(prompt, preset, seed, model)
        else:
            scene_payload, tokens_in, tokens_out = _generate_python_scene(prompt, preset, seed, model)
    except SceneGenError as e:
        body = {"error": e.message}
        if e.scene is not None:
            body["scene_code"] = e.scene
        return jsonify(body), e.status

    # render — in-process sandbox (local) or the network-less worker (worker mode)
    img_id   = uuid.uuid4().hex
    filename = f"{img_id}.png"

    scene_text = _scene_text(scene_payload)
    try:
        result = _render_scene(
            scene_payload, filename=filename,
            width=width, height=height, seed=seed, preset=preset
        )
    except SceneValidationError as e:
        return jsonify({"error": f"Scene rejected by sandbox: {e}", "scene_code": scene_text}), 400
    except sandbox.RenderTimeout as e:
        return jsonify({"error": f"Render timed out: {e}"}), 400
    except sandbox.RenderError as e:
        return jsonify({"error": f"Render error: {e}", "scene_code": scene_text}), 500
    except Exception as e:
        return jsonify({"error": f"Render error: {e}", "scene_code": scene_text}), 500

    thumb_name = os.path.basename(result["thumb"]) if result.get("thumb") else None
    svg_name   = os.path.basename(result["svg"])   if result.get("svg")   else None

    created_at = datetime.utcnow().isoformat() + "Z"
    db = get_db()
    db.execute("""
        INSERT INTO images (id, filename, thumb, prompt, preset, seed, width, height,
                            tokens_in, tokens_out, model, scene_code, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (img_id, filename, thumb_name, prompt, preset, seed, width, height,
          tokens_in, tokens_out, model, scene_text, created_at))
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
