# Deploying Pillow Polygons

A hardened, two-container setup for running the app on a portfolio site. It
layers **network isolation** and **container** isolation on top of the
**application-level** scene-code sandbox in the code (AST allowlist + restricted
builtins + subprocess rlimits — see the README "Security" section).

## Architecture — the render path has no network

```
            ┌─────────────────┐   shared volume    ┌──────────────────────────┐
 client ───▶│  web            │   /jobs (queue)    │  render  (network_mode:  │
   (TLS     │  serves HTTP,   │ ─────jobs─────────▶│           none)          │
    proxy)  │  calls Claude,  │                    │  the ONLY place scene    │
            │  validates code │◀────results────────│  code runs. seccomp +    │
            │  RENDER_MODE=   │   /app/static/      │  read-only + no caps     │
            │     worker      │   renders (shared)  │                          │
            └─────────────────┘                    └──────────────────────────┘
```

`web` needs the network (HTTP + the Anthropic API) so it must **not** execute
scene code. It hands each render to `render` over a filesystem job queue and
waits. `render` runs with `network_mode: none` — no network stack at all — so
even a sandbox escape in CPython/Pillow has nowhere to phone home, no access to
the web tier, and no API key. See `jobqueue.py` / `render_worker.py`.

## Quick start

```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY
docker compose up -d --build
# web listens on 127.0.0.1:8040 — put a TLS reverse proxy in front (below)
```

Verify:

```bash
curl -s http://127.0.0.1:8040/api/tags        # -> [] (200)
docker compose ps                              # web + render both up
docker compose logs -f render                  # "render_worker: watching ..."
```

## What the container hardening adds

| Control | web | render | Why |
|---|:--:|:--:|---|
| Non-root (`user: 10001`) | ✅ | ✅ | No root inside the container. |
| Read-only rootfs | ✅ | ✅ | Image filesystem is immutable; a compromise can't persist. |
| Dropped capabilities (`cap_drop: ALL`) | ✅ | ✅ | Neither service needs any Linux capability. |
| `no-new-privileges` | ✅ | ✅ | setuid binaries can't raise privileges. |
| Resource limits (`cpus`/`mem_limit`/`pids_limit`) | ✅ | ✅ | Caps blast radius, layered under the in-process `rlimits`. |
| **No network** (`network_mode: none`) | — | ✅ | The render path can't reach the network at all. |
| **Custom seccomp** (`seccomp/render-worker.json`) | — | ✅ | Deny-by-default allowlist; networking syscalls omitted, so even `socket()` fails. |
| Default seccomp | ✅ | — | web keeps Docker's default profile (it needs sockets). |

Verified end-to-end: `web` boots healthy on a read-only rootfs with the DB on
its volume; `render` boots under the custom seccomp profile and `--network none`;
a job submitted from `web` is rendered by `render` and the image appears in the
shared renders volume; an `import os` injection is rejected; and `socket()`
inside `render` fails with `PermissionError` (seccomp) on top of having no
network interfaces.

### Render mode

`RENDER_MODE=worker` (set for `web` in compose) routes renders through the queue.
The default, `RENDER_MODE=local`, runs the in-process subprocess sandbox instead
— handy for `python app.py` development and the test suite, with no worker needed.

### seccomp portability

`seccomp/render-worker.json` is a deny-by-default allowlist curated and verified
on CPython 3.12 + Pillow + multiprocessing-fork on x86_64/glibc. It is
kernel/glibc/arch-sensitive. If a render fails on your platform, `strace` the
worker for the `EPERM` syscall and add it — or drop the `seccomp:...` line from
the `render` service to fall back to `--network none` + Docker's default seccomp,
which still fully isolates the render path from the network.

## Reverse proxy + real client IPs

The container publishes to `127.0.0.1` only. Terminate TLS with a proxy
(Caddy/nginx/Traefik) in front, and set `TRUST_PROXY=1` (already set in compose)
so the per-IP rate limits read the real client IP from `X-Forwarded-For` instead
of the proxy's address. Example Caddy:

```
polygons.example.com {
    reverse_proxy 127.0.0.1:8040
}
```

## Environment variables

`ANTHROPIC_API_KEY` is required (via `.env`). All anti-abuse and sandbox knobs
are documented in the README; the deployment-relevant additions are:

| Var | Default | Meaning |
|-----|---------|---------|
| `POLY_DB_PATH` | `<app>/poly.db` | SQLite location — pointed at the `/data` volume so the rootfs can stay read-only. |
| `RENDER_CPU_SECONDS` / `RENDER_MEM_MB` / `RENDER_WALL_SECONDS` / `RENDER_FSIZE_MB` | `10` / `1024` / `20` / `64` | Per-render subprocess limits. |
| `RENDER_CONCURRENCY` | `1` | Max simultaneous render subprocesses in the worker (see memory budget below). |
| `RENDER_MODE` | `local` | `worker` routes renders to the network-less worker (compose sets this on `web`); `local` runs the in-process sandbox. |
| `JOBS_DIR` | `/jobs` | Shared job-queue directory (mounted into both `web` and `render`). |

> **Memory budget — keep these consistent.** Renders run in the `render`
> container, serialized by `RENDER_CONCURRENCY` (default 1), so the worst-case
> render footprint is a single `RENDER_MEM_MB`. Size the `render` service so
> `mem_limit ≥ RENDER_MEM_MB × RENDER_CONCURRENCY + ~512 MiB` headroom — with the
> defaults that's `1024 × 1 + 512 → 1536m`. This guarantees the per-render
> `RLIMIT_AS` returns a controlled "exceeded memory limit" error *before* the
> container OOM-killer restarts the worker. `web` no longer renders, so it runs
> with a smaller `768m`.

> **Scaling note:** `web` runs **one** gunicorn worker (with threads) on purpose
> — the rate-limit and form-token state is in-process. Running multiple
> web workers/replicas needs a shared store (Redis); see the README. The `render`
> worker can be scaled independently (each instance claims jobs atomically).

## Network/syscall isolation of the render path (issue #2 — done)

Scene code now executes **only** in the `render` service, which runs with
`network_mode: none` and the deny-by-default seccomp profile in
[`seccomp/render-worker.json`](seccomp/render-worker.json) (networking syscalls
omitted). The web tier never `exec()`s scene code; it hands jobs over the shared
`/jobs` queue and waits. This closes the residual gap from the earlier
single-container setup, where the render subprocess inherited the web container's
network namespace. See the architecture diagram above and the seccomp portability
note for tuning on other kernels/arches.
