"""
anti_abuse.py — self-contained abuse mitigations for Pillow Polygons.

Covers the four areas the owner asked for:
  1. Rate limiting / throttling           -> @rate_limit, generate_rate_limiters()
  2. Upload / resource abuse              -> validate_image_upload(), safe_int()
  3. Spam / bot submissions               -> honeypot + signed form-token + captcha hook
  4. NSFW / illegal-image warning         -> moderate_prompt(), moderate_image()

Design constraints (see ANTI_ABUSE_TASK / README):
  - No heavy deps. Flask + Pillow + anthropic + stdlib only.
  - Import-safe: importing this module has NO side effects. The only global
    mutation (PIL's MAX_IMAGE_PIXELS) happens inside the explicit init(app) call,
    so the module stays unit-testable.
  - Everything env-tunable; defaults must not change behavior for a legit single user.

Optional upgrades intentionally NOT hard-required (left as comments):
  - Flask-Limiter + Redis for rate limits that share state across gunicorn workers.
  - Cloudflare Turnstile / hCaptcha for real bot defense (hook is wired below).
  - A real CSAM hash-matching service (PhotoDNA / cloud CSAI match) + a reporting
    path. The keyword/model passes here are NOT a substitute for that — see §4.
"""

import os
import io
import time
import hmac
import hashlib
import threading
from collections import defaultdict, deque
from functools import wraps

from flask import request, jsonify
from PIL import Image


# ── env helpers ──────────────────────────────────────────────────────────────
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Read at call time (not import time) so tests / deployments can set env freely.
def _trust_proxy():            return _env_flag("TRUST_PROXY")
def _generate_per_min():       return _env_int("GENERATE_RATE_PER_MIN", 5)
def _generate_per_day():       return _env_int("GENERATE_RATE_PER_DAY", 50)
def _mutate_per_min():         return _env_int("MUTATE_RATE_PER_MIN", 60)
def _max_upload_bytes():       return _env_int("MAX_UPLOAD_MB", 16) * 1024 * 1024
def _moderation_enabled():     return _env_flag("MODERATION_ENABLED")

# Decompression-bomb / DoS guards.
MAX_IMAGE_PIXELS = 40_000_000       # ~6300x6300; Pillow raises DecompressionBombError above 2x this
MAX_DIMENSION    = 8_000            # reject either side larger than this
ALLOWED_FORMATS  = ("JPEG", "PNG", "GIF", "WEBP")

# Numeric clamps for /api/generate.
DIM_MIN, DIM_MAX   = 256, 2048
SEED_MIN, SEED_MAX = 0, 2_147_483_647


def init(app):
    """Explicit, side-effecting setup. Call once from app.py after creating `app`.

    - Pins Pillow's decompression-bomb ceiling.
    - Syncs Flask's MAX_CONTENT_LENGTH to MAX_UPLOAD_MB (keeps the existing 16MB
      default, but lets it be tuned by env).
    """
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    app.config["MAX_CONTENT_LENGTH"] = _max_upload_bytes()
    return app


# ── 0. shared utilities ──────────────────────────────────────────────────────
def safe_int(value, default, lo, hi):
    """Parse an int from untrusted input and clamp to [lo, hi]. Never raises."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def client_ip():
    """Best-effort client IP.

    Only trust X-Forwarded-For when TRUST_PROXY=1 — otherwise any client can
    spoof the header to dodge per-IP limits. With the flag set we take the
    left-most (original client) entry of the chain.
    """
    if _trust_proxy():
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ── 1. rate limiting ─────────────────────────────────────────────────────────
# In-memory sliding-window counters keyed by (scope, client-ip).
# TODO: back with Redis if you run >1 worker — in-memory limits do NOT share
#       across gunicorn workers, so each worker would allow `limit` independently.
#       (Flask-Limiter with a Redis storage backend is the drop-in upgrade.)
_buckets = defaultdict(deque)   # key -> deque[float timestamps]
_buckets_lock = threading.Lock()


def _hit(scope, limit, window_seconds):
    """Record a hit; return retry_after_seconds if over the limit, else None."""
    now = time.time()
    key = (scope, client_ip())
    with _buckets_lock:
        dq = _buckets[key]
        cutoff = now - window_seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = int(dq[0] + window_seconds - now) + 1
            return max(retry_after, 1)
        dq.append(now)
        # Opportunistic cleanup so idle keys don't accumulate forever.
        if not dq:
            _buckets.pop(key, None)
    return None


def rate_limit(limit, window_seconds, scope="default"):
    """Decorator: sliding-window per-IP limit. 429 + Retry-After + JSON on excess.

    Stack multiple decorators for layered limits (e.g. per-minute AND per-day);
    each must use a distinct `scope` so their buckets don't collide.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            retry_after = _hit(scope, limit, window_seconds)
            if retry_after is not None:
                resp = jsonify({"error": "Rate limit exceeded. Slow down and try again."})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry_after)
                return resp
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def generate_rate_limiters():
    """Strict layered limit for the expensive /api/generate endpoint.

    Returns a single decorator stacking per-minute + per-day caps. Apply it
    closest to the view function so spam checks/Claude calls never run when limited.
    """
    per_min = rate_limit(_generate_per_min(), 60,     scope="generate_min")
    per_day = rate_limit(_generate_per_day(), 86_400, scope="generate_day")

    def combined(fn):
        return per_min(per_day(fn))
    return combined


def mutate_rate_limit(fn):
    """Looser limit for cheap, local-state-mutating endpoints."""
    return rate_limit(_mutate_per_min(), 60, scope="mutate")(fn)


# ── 2. upload / resource abuse ───────────────────────────────────────────────
class UploadRejected(Exception):
    """Raised when an upload fails validation. status is the HTTP code to return."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# detected PIL format -> (output PIL format, file ext, mime)
_FORMAT_OUT = {
    "JPEG": ("JPEG", "jpg",  "image/jpeg"),
    "PNG":  ("PNG",  "png",  "image/png"),
    "GIF":  ("PNG",  "png",  "image/png"),   # flatten animated GIF to a single clean PNG
    "WEBP": ("WEBP", "webp", "image/webp"),
}


class CleanImage:
    """A validated, re-encoded upload. The caller persists it via .save(path)."""
    def __init__(self, image, src_format, out_format, ext, mime):
        self.image      = image        # PIL.Image in RGB, already re-encoded in memory
        self.src_format = src_format   # what the bytes actually were
        self.out_format = out_format   # PIL format we re-save as
        self.ext        = ext          # real extension, e.g. "png"
        self.mime       = mime         # real mime, e.g. "image/png"

    def save(self, path):
        kwargs = {}
        if self.out_format == "JPEG":
            kwargs["quality"] = 90
        self.image.save(path, format=self.out_format, **kwargs)
        return path


def validate_image_upload(file_storage):
    """Validate + sanitize an uploaded image. Returns CleanImage or raises UploadRejected.

    Steps: size cap -> integrity verify -> format allowlist (NOT the filename) ->
    decompression-bomb guard -> dimension cap -> re-encode (strips EXIF/ICC/
    trailing data/polyglots). We never persist the raw uploaded bytes.
    """
    raw = file_storage.read()
    if not raw:
        raise UploadRejected("Empty upload.")
    if len(raw) > _max_upload_bytes():
        raise UploadRejected(
            f"Image too large (max {_max_upload_bytes() // (1024 * 1024)}MB).", status=413
        )

    # 1) integrity check — .verify() detects truncated/corrupt files but leaves
    #    the image unusable afterward, so we re-open from the same bytes below.
    try:
        Image.open(io.BytesIO(raw)).verify()
    except Image.DecompressionBombError:
        raise UploadRejected("Image rejected: decompression bomb.")
    except Exception:
        raise UploadRejected("Not a valid image file.")

    # 2) re-open for real (and for metadata) — trust the decoded format, never the name.
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Image.DecompressionBombError:
        raise UploadRejected("Image rejected: decompression bomb.")
    except Exception:
        raise UploadRejected("Not a valid image file.")

    fmt = (img.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise UploadRejected(
            f"Unsupported image type ({fmt or 'unknown'}). Allowed: JPEG, PNG, GIF, WEBP."
        )

    w, h = img.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise UploadRejected(
            f"Image too large in pixels ({w}x{h}); max {MAX_DIMENSION}px per side."
        )

    # 3) re-encode to a clean image (flattens to RGB, dropping EXIF/ICC/alpha/
    #    trailing bytes and any polyglot payload).
    out_format, ext, mime = _FORMAT_OUT[fmt]
    clean = img.convert("RGB")
    return CleanImage(clean, src_format=fmt, out_format=out_format, ext=ext, mime=mime)


# ── 3. spam / bot submissions ────────────────────────────────────────────────
HONEYPOT_FIELD = "website"   # hidden in the UI; humans never fill it, bots often do
_FORM_TOKEN_MIN_AGE = 2      # seconds — submissions faster than this are bots
_FORM_TOKEN_MAX_AGE = 12 * 60 * 60  # 12h — generous so a long-open tab still works

_ephemeral_secret = None
_ephemeral_lock = threading.Lock()


def _form_secret():
    """HMAC key for form tokens. Prefer FORM_TOKEN_SECRET; otherwise use a random
    per-process secret (fine for a single process — tokens just won't survive a
    restart, which only forces a page reload)."""
    s = os.environ.get("FORM_TOKEN_SECRET")
    if s:
        return s.encode("utf-8")
    global _ephemeral_secret
    if _ephemeral_secret is None:
        with _ephemeral_lock:
            if _ephemeral_secret is None:
                _ephemeral_secret = os.urandom(32)
    return _ephemeral_secret


def make_form_token():
    """Issue a signed, timestamped token to embed in the page at load time."""
    ts = str(int(time.time()))
    sig = hmac.new(_form_secret(), ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_form_token(token):
    """Return (ok: bool, reason: str). Rejects forged, stale, or too-fast tokens."""
    if not token or "." not in token:
        return False, "missing or malformed form token"
    ts_str, sig = token.split(".", 1)
    expected = hmac.new(_form_secret(), ts_str.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False, "invalid form token signature"
    try:
        ts = int(ts_str)
    except ValueError:
        return False, "invalid form token timestamp"
    age = time.time() - ts
    if age < _FORM_TOKEN_MIN_AGE:
        return False, "submitted too fast (likely a bot)"
    if age > _FORM_TOKEN_MAX_AGE:
        return False, "form token expired — reload the page"
    return True, "ok"


def check_honeypot(form):
    """True if the honeypot field was filled (i.e. this looks like a bot)."""
    return bool((form.get(HONEYPOT_FIELD) or "").strip())


def verify_captcha(token):
    """Opt-in CAPTCHA hook. Passes when CAPTCHA_SECRET is unset (so it's off by
    default). When set, verifies a Cloudflare Turnstile token via stdlib urllib.

    Swap the siteverify URL/field names for hCaptcha/reCAPTCHA if you prefer.
    """
    secret = os.environ.get("CAPTCHA_SECRET")
    if not secret:
        return True  # disabled
    if not token:
        return False
    try:
        import json
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"secret": secret, "response": token}).encode()
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return bool(json.loads(resp.read().decode()).get("success"))
    except Exception:
        # Fail closed when a captcha is configured but verification errors out.
        return False


# ── 4. NSFW / illegal-image moderation ───────────────────────────────────────
# Cheap, always-on first pass: an illegal-category keyword denylist focused on
# CSAM. These terms are never part of legitimate single-user art prompts, so
# blocking them does not change behavior for real users. This is deliberately
# narrow (illegal only), NOT a general NSFW filter — that's the model pass below.
#
# !!! HONEST LIMITATION !!!
# A keyword list + an LLM classifier is NOT real CSAM detection and must not be
# treated as one. Any deployment that accepts third-party uploads at scale needs
# a hash-matching service (PhotoDNA / a cloud CSAI-match API) plus a mandated
# reporting path (e.g. NCMEC).
# TODO: integrate a real CSAM hash-match + reporting pipeline before opening
#       uploads to the public.
_CSAM_PHRASES = (
    "child porn", "child pornography", "childporn", "csam", "cp video",
    "preteen sex", "pedophilia", "pedophile", "lolicon", "shotacon",
    "underage porn", "underage nude", "underage nudes", "jailbait",
)
_MINOR_TOKENS  = ("child", "children", "kid", "kids", "toddler", "infant",
                  "minor", "minors", "preteen", "pre-teen", "underage",
                  "11yo", "12yo", "13yo", "10yo", "9yo", "8yo")
_SEXUAL_TOKENS = ("nude", "naked", "porn", "sex", "sexual", "explicit",
                  "nsfw", "erotic", "fellatio", "genital", "topless")


def _allow():               return {"action": "allow", "reason": ""}
def _block(reason):         return {"action": "block", "reason": reason}
def _flag(reason):          return {"action": "flag",  "reason": reason}


def _keyword_verdict(text):
    low = (text or "").lower()
    for phrase in _CSAM_PHRASES:
        if phrase in low:
            return _block("Prompt matches a prohibited illegal-content term.")
    # Combination heuristic: a minor reference together with a sexual term.
    if any(m in low for m in _MINOR_TOKENS) and any(s in low for s in _SEXUAL_TOKENS):
        return _block("Prompt combines references to minors with sexual content.")
    return _allow()


def _model_classify_text(text, client):
    """Optional LLM classification. Returns a verdict dict. Fails OPEN (allow) on
    any API error so a transient hiccup never blocks a legit generate."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=20,
            system=(
                "You are a content-safety classifier for an art-generation prompt. "
                "Reply with exactly one word: SAFE, NSFW, or ILLEGAL. "
                "ILLEGAL = sexual content involving minors, or other clearly illegal "
                "imagery. NSFW = adult sexual/explicit content. SAFE = everything else."
            ),
            messages=[{"role": "user", "content": text[:2000]}],
        )
        verdict = resp.content[0].text.strip().upper()
        if "ILLEGAL" in verdict:
            return _block("Classifier flagged the prompt as illegal content.")
        if "NSFW" in verdict:
            return _flag("Classifier flagged the prompt as NSFW.")
        return _allow()
    except Exception:
        return _allow()


def moderate_prompt(text, client=None):
    """Moderate a text prompt. Keyword denylist always runs; the model pass runs
    only when MODERATION_ENABLED=1 and an anthropic client is supplied."""
    verdict = _keyword_verdict(text)
    if verdict["action"] == "block":
        return verdict
    if _moderation_enabled() and client is not None:
        return _model_classify_text(text, client)
    return verdict


def moderate_image(pil_image, client=None):
    """Moderate an uploaded reference image via Claude vision. No-op (allow) unless
    MODERATION_ENABLED=1 and a client is supplied. Fails OPEN on API errors."""
    if not _moderation_enabled() or client is None or pil_image is None:
        return _allow()
    try:
        import base64
        buf = io.BytesIO()
        pil_image.convert("RGB").save(buf, format="JPEG", quality=80)
        b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=20,
            system=(
                "You are a content-safety classifier for an uploaded reference image. "
                "Reply with exactly one word: SAFE, NSFW, or ILLEGAL. "
                "ILLEGAL = sexual content involving minors or other clearly illegal "
                "imagery. NSFW = adult sexual/explicit content. SAFE = everything else."
            ),
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": "Classify this image."},
            ]}],
        )
        verdict = resp.content[0].text.strip().upper()
        if "ILLEGAL" in verdict:
            return _block("Reference image flagged as illegal content.")
        if "NSFW" in verdict:
            return _flag("Reference image flagged as NSFW.")
        return _allow()
    except Exception:
        return _allow()
