# Pillow Polygons

A small Flask app that turns a text prompt into polygon art. The prompt is sent
to the Anthropic API, which returns Python
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
| **Text-only API** | The endpoint takes a prompt and a few small form fields — **no file uploads**. Request bodies are capped at 1MB, so there's no upload surface to abuse (no decompression bombs, polyglots, or EXIF tricks). |
| **Spam / bots** | A hidden honeypot field, a signed+timestamped form token (rejects forged or impossibly-fast submissions), and an opt-in CAPTCHA hook (Cloudflare Turnstile). |
| **NSFW / illegal** | An always-on keyword denylist for illegal categories (CSAM) blocks the prompt; an opt-in Claude classifier screens the prompt text. A visible policy notice + required acknowledgement checkbox gates every generate. |

Numeric inputs (`width`, `height`, `seed`) are parsed and clamped, so
`seed=abc` no longer 500s and `width=999999` can't DoS the renderer.

**Cost visibility.** Every generation records the model used and reports a
`cost_usd` (derived from the model's list price × token counts; see `PRICING` /
`compute_cost` in `app.py`). It's returned from `/api/generate` and `/api/images`
and shown in the UI's token meter, so per-render Anthropic spend is visible at a
glance. The `model` column auto-migrates onto existing databases; legacy rows
with no recorded model price at the Sonnet tier.

### Environment variables

All optional — defaults preserve current behavior for a single user.

| Var | Default | Meaning |
|-----|---------|---------|
| `TRUST_PROXY` | unset (off) | When `1`, read the client IP from `X-Forwarded-For` (left-most). Only enable behind a trusted proxy — otherwise clients spoof it to bypass limits. |
| `GENERATE_RATE_PER_MIN` | `5` | Max `/api/generate` calls per IP per minute. |
| `GENERATE_RATE_PER_DAY` | `50` | Max `/api/generate` calls per IP per day. |
| `MUTATE_RATE_PER_MIN` | `60` | Per-IP-per-minute cap on the mutating image/tag/folder endpoints. |
| `MODERATION_ENABLED` | unset (off) | When `1`, also run the Claude classifier on prompts. Off by default so latency/cost are unchanged. |
| `CAPTCHA_SECRET` | unset (off) | Server-side Turnstile secret. When set, `/api/generate` requires a valid `captcha_token`. **Must be set together with `CAPTCHA_SITEKEY`** — otherwise the page renders no widget and every browser request fails. |
| `CAPTCHA_SITEKEY` | unset | Client-side Turnstile site key. When set, the page renders the Turnstile widget and the JS sends its token as `captcha_token`. |
| `FORM_TOKEN_SECRET` | random per-process | HMAC key for anti-bot form tokens. Set a stable value if you run >1 process or want tokens to survive restarts. |
| `RENDER_CPU_SECONDS` | `10` | CPU-time limit for the scene-render subprocess. |
| `RENDER_MEM_MB` | `1024` | Address-space (memory) limit for the render subprocess. |
| `RENDER_WALL_SECONDS` | `20` | Wall-clock timeout; a render exceeding it is killed. |
| `RENDER_FSIZE_MB` | `64` | Max output file size the render subprocess may write. |
| `RENDER_CONCURRENCY` | `1` | Max simultaneous render subprocesses. Keeps worst-case render memory at one `RENDER_MEM_MB` (vs. `threads × RENDER_MEM_MB`); size the container memory accordingly. |
| `RENDER_MODE` | `local` | `worker` routes scene execution to the network-less render worker (see `DEPLOY.md`); `local` runs the in-process subprocess sandbox. |
| `JOBS_DIR` | `/jobs` | Shared job-queue directory used when `RENDER_MODE=worker`. |
| `SCENE_FORMAT` | `python` | `python` asks the model for Pillow scene **code** (expressive, exec'd, sandboxed). `json` asks for a declarative JSON **scene** drawn by a fixed interpreter — no code execution at all. See "Scene format" below. |

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
   `().__class__.__subclasses__()` escape), the **frame/generator/coroutine
   introspection attributes** (`gi_frame`, `f_back`, `f_globals`, `cr_frame`, …
   — the non-dunder escape where a running generator's frame walks out to
   `f_globals['os']`), `str.format`/`format_map` (which traverse attributes named
   inside a format-string literal the AST can't see), and dangerous builtin names
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

**C. Network-less worker (deployment, `RENDER_MODE=worker`)**
In the container deployment, scene code executes **only** in a dedicated
`render` service running with `network_mode: none`, a deny-by-default seccomp
profile, a read-only rootfs, and dropped capabilities. The web tier (which needs
the network for HTTP + the Anthropic API) never `exec()`s scene code — it hands
jobs to the worker over a shared filesystem queue (`jobqueue.py` /
`render_worker.py`). So even a kernel-level escape from layers A/B has no network
and no access to the web tier or its API key. Set `RENDER_MODE=local` (the
default) to run the in-process sandbox instead — used for development and tests.
See `DEPLOY.md`.

> **The in-language allowlist is a speed bump, not a boundary.** CPython
> introspection is bypassable in principle — one escape class (the generator
> frame walk) was already found and fixed. Layer A exists to make casual escapes
> hard and to fail a slipped model output fast; the controls that actually
> *hold* a determined attacker are Layer B (resource limits) and Layer C (no
> network + seccomp + read-only rootfs). Treat A as defense-in-depth, not the wall.

### Scene format: code vs. data (`SCENE_FORMAT`)

Everything above (layers A–C) exists to *contain* the fact that the default path
**executes model-generated Python**. `SCENE_FORMAT=json` sidesteps the problem
instead of containing it: the model returns a declarative JSON scene, and
`scene_json.py` draws it with a fixed set of primitives (`polygon`, `ellipse`,
`rectangle`, `line`, `arc`, `point`, `text`, plus `gradient`/`grain`/`vignette`).
There is no `exec`, no `eval`, and no attribute access — so the prompt-injection
→ RCE class **does not exist** on this path. The threat model shrinks to *data*
validation: caps on layer/op/point counts and grain size (enforced in
`validate_scene_json`), with the subprocess resource limits kept as
defense-in-depth around Pillow itself.

The trade-off is expressiveness. Python scene code is Turing-complete — the look
comes partly from *computation* (procedural placement, arithmetic gradients,
`rng`-driven variation). JSON can only use the primitives the renderer
implements, so output trends toward "what the vocabulary allows." Both paths are
wired through the same sandbox, worker queue, cost accounting, and SVG twin, so
you can run them side by side (`SCENE_FORMAT=json python app.py`) and compare.

| | `python` (default) | `json` |
|---|---|---|
| Model emits | Pillow scene **code** | declarative **JSON** scene |
| Executed by | `exec()` in `renderer.render()` | interpreted by `scene_json.paint()` |
| RCE surface | yes — needs the full sandbox stack | **none** (no code runs) |
| Threat model | contain arbitrary code | validate data (size caps) |
| Expressiveness | Turing-complete | fixed primitive set |
| Retries | syntax check + AST sandbox | JSON parse + schema check |

**Recommendation:** `json` is the more durable boundary for untrusted/public use
(safe by construction); keep `python` when you want maximum procedural richness
on trusted input. This is a prototype of the data-not-code approach — the
primitive set is intentionally small and can grow.

### Stronger isolation if this takes real public traffic

The current jail (layers A–C) contains an attacker who reaches `os`/`socket`:
no network, no caps, read-only FS, a syscall allowlist. What it does **not**
contain is a **kernel-level** escape (a Linux LPE through a syscall that is on
the seccomp allowlist). For a single-user portfolio app that is an accepted risk.
If Pillow Polygons ever takes untrusted public traffic at volume, the next step
is a syscall-filtered / virtualized jail so a kernel bug is contained too:

- **nsjail** with a tight seccomp-bpf policy (cheap, same host),
- **gVisor** (`runsc`) — a user-space kernel intercepting syscalls, or
- a **Firecracker** microVM per render — strongest, with a real VM boundary.

Recorded here so the decision is explicit; not needed at single-user scale.

### CSAM detection is not real here

The keyword denylist + LLM classifier in `anti_abuse.py` are a deterrent, **not**
real CSAM detection. The API is text-only, so there's no uploaded-image surface;
but any deployment serving generated imagery at scale still needs a real
hash-matching service (PhotoDNA / a cloud CSAI-match API) plus a mandated
reporting path (e.g. NCMEC). This is marked as a `TODO` in the code, not faked.
