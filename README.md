# Pillow Polygons

A small Flask app that turns a text prompt (+ optional reference image) into
polygon art. The prompt is sent to the Anthropic API, which returns Python
"scene code"; `renderer.py` executes that code with Pillow to draw the image.
Results are stored in SQLite (`poly.db`) and written to `static/renders/`.

## Run

```bash
pip install flask pillow anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python app.py            # http://127.0.0.1:8040
```

## Anti-abuse

Abuse mitigations live in a self-contained module, `anti_abuse.py`, wired into
`app.py` with minimal edits. They cover four areas and are designed to be
**invisible to a legitimate single user** while shutting down the cheap attacks:

| Area | What it does |
|------|--------------|
| **Rate limiting** | In-memory sliding-window per client IP. Strict cap on the expensive `POST /api/generate` (per-minute **and** per-day), looser cap on the cheap mutating endpoints. Returns `429` + `Retry-After`. |
| **Upload abuse** | `validate_image_upload()` enforces a size cap, verifies the bytes are really an image (`Image.verify()`, format allowlist — **not** the filename), guards against decompression bombs (`MAX_IMAGE_PIXELS` + max dimension), then **re-encodes** to a clean image (strips EXIF/ICC/trailing data/polyglots). Uploaded refs are deleted after the request. |
| **Spam / bots** | A hidden honeypot field, a signed+timestamped form token (rejects forged or impossibly-fast submissions), and an opt-in CAPTCHA hook (Cloudflare Turnstile). |
| **NSFW / illegal** | An always-on keyword denylist for illegal categories (CSAM) blocks the prompt; an opt-in Claude classifier (prompt + reference image) adds NSFW/illegal screening. A visible policy notice + required acknowledgement checkbox gates every generate. |

Numeric inputs (`width`, `height`, `seed`) are parsed and clamped, so
`seed=abc` no longer 500s and `width=999999` can't DoS the renderer.

### Environment variables

All optional — defaults preserve current behavior for a single user.

| Var | Default | Meaning |
|-----|---------|---------|
| `TRUST_PROXY` | unset (off) | When `1`, read the client IP from `X-Forwarded-For` (left-most). Only enable behind a trusted proxy — otherwise clients spoof it to bypass limits. |
| `GENERATE_RATE_PER_MIN` | `5` | Max `/api/generate` calls per IP per minute. |
| `GENERATE_RATE_PER_DAY` | `50` | Max `/api/generate` calls per IP per day. |
| `MUTATE_RATE_PER_MIN` | `60` | Per-IP-per-minute cap on the mutating image/tag/folder endpoints. |
| `MAX_UPLOAD_MB` | `16` | Max upload size (also sets Flask's `MAX_CONTENT_LENGTH`). |
| `MODERATION_ENABLED` | unset (off) | When `1`, also run the Claude classifier on prompts (and reference images). Off by default so latency/cost are unchanged. |
| `CAPTCHA_SECRET` | unset (off) | Server-side Turnstile secret. When set, `/api/generate` requires a valid `captcha_token`. **Must be set together with `CAPTCHA_SITEKEY`** — otherwise the page renders no widget and every browser request fails. |
| `CAPTCHA_SITEKEY` | unset | Client-side Turnstile site key. When set, the page renders the Turnstile widget and the JS sends its token as `captcha_token`. |
| `FORM_TOKEN_SECRET` | random per-process | HMAC key for anti-bot form tokens. Set a stable value if you run >1 process or want tokens to survive restarts. |
| `RENDER_CPU_SECONDS` | `10` | CPU-time limit for the scene-render subprocess. |
| `RENDER_MEM_MB` | `1024` | Address-space (memory) limit for the render subprocess. |
| `RENDER_WALL_SECONDS` | `20` | Wall-clock timeout; a render exceeding it is killed. |
| `RENDER_FSIZE_MB` | `64` | Max output file size the render subprocess may write. |
| `RENDER_CONCURRENCY` | `1` | Max simultaneous render subprocesses. Keeps worst-case render memory at one `RENDER_MEM_MB` (vs. `threads × RENDER_MEM_MB`); size the container memory accordingly. |

### Optional upgrades (not hard-required)

- **Redis / Flask-Limiter** — the in-memory rate limiter does **not** share state
  across gunicorn workers; each worker allows the limit independently. Back it
  with Redis (or Flask-Limiter) if you run more than one worker.
- **Real CAPTCHA** — the Turnstile hook is wired; swap in hCaptcha/reCAPTCHA by
  changing the siteverify URL/fields in `verify_captcha()`.
- **Real CSAM detection** — see the security note below.

---

## Security: scene-code execution sandbox

`renderer.py` executes Claude-generated scene code. Because that code is
produced from a **user-controlled prompt**, it is a prompt-injection → remote-
code-execution surface (a crafted prompt could try to make the model emit
`import os; os.system('...')`). This is sandboxed in two layers:

**A. Restricted execution (`renderer.py`)**
1. `validate_scene()` runs an **AST allowlist** before exec: it rejects
   `import`/`from-import`, any dunder attribute access (blocks the
   `().__class__.__subclasses__()` escape), and dangerous builtin names
   (`eval`, `exec`, `open`, `__import__`, `getattr`, `globals`, …).
2. Scene code runs with a **curated `__builtins__`** (`SAFE_BUILTINS`) — no
   `__import__`/`open`/`eval`/`exec` — so even code that slipped past the AST
   scan can't reach `os`, `socket`, or the filesystem. (No `import` ⇒ no
   `socket` ⇒ network access is denied at the language level.)
3. In `app.py`, a rejected scene triggers one Claude retry (asking it to remove
   the construct), mirroring the syntax-error retry; a persistent violation
   returns a `400`.

**B. Subprocess sandbox (`sandbox.py`)**
The render runs in a **child process** with POSIX `rlimits` (CPU time, address
space, output file size) and a **wall-clock timeout**. A runaway or hostile
scene (infinite loop, memory bomb, segfault, OOM-kill) is contained and killed
without taking down the Flask process. All limits are env-tunable
(`RENDER_CPU_SECONDS`, `RENDER_MEM_MB`, `RENDER_WALL_SECONDS`, `RENDER_FSIZE_MB`).

**Residual risk / further hardening.** Layer A denies network/filesystem access
at the language level, but kernel-level isolation (a seccomp syscall filter, a
network namespace, a read-only root FS) needs root or a container and is a
deployment concern. For production, also run the render child inside a
locked-down container/netns. This is noted as a `TODO` in `sandbox.py`.

### CSAM detection is not real here

The keyword denylist + LLM classifier in `anti_abuse.py` are a deterrent, **not**
real CSAM detection. Any deployment that accepts third-party uploads at scale
needs a hash-matching service (PhotoDNA / a cloud CSAI-match API) plus a
mandated reporting path (e.g. NCMEC). This is marked as a `TODO` in the code,
not faked.
