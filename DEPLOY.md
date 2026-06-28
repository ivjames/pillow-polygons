# Deploying Pillow Polygons

A hardened container setup for running the app on a portfolio site. It layers
**container** isolation on top of the **application-level** scene-code sandbox
already in the code (AST allowlist + restricted builtins + subprocess rlimits —
see the README "Security" section).

## Quick start

```bash
cp .env.example .env          # add your ANTHROPIC_API_KEY
docker compose up -d --build
# app listens on 127.0.0.1:8040 — put a TLS reverse proxy in front (below)
```

Verify:

```bash
curl -s http://127.0.0.1:8040/api/tags        # -> [] (200)
docker compose logs -f web
```

## What the container hardening adds

`docker-compose.yml` runs the web service with:

| Control | Setting | Why |
|---|---|---|
| Non-root | `user: 10001:10001` | No root inside the container. |
| Read-only rootfs | `read_only: true` | The image filesystem is immutable; a compromise can't persist. |
| Writable surfaces only where needed | `tmpfs: /tmp`, `tmpfs /app/static/uploads`; volumes for `/data` (DB) + `/app/static/renders` | Uploads are ephemeral (deleted post-request); DB + gallery persist. |
| Dropped capabilities | `cap_drop: [ALL]` | The app needs no Linux capabilities. |
| No privilege escalation | `no-new-privileges:true` | setuid binaries can't raise privileges. |
| Default seccomp | (applied automatically by Docker) | Blocks the dangerous syscalls (`ptrace`, `mount`, kernel-module ops, …). |
| Resource limits | `cpus`, `mem_limit`, `pids_limit` | Caps blast radius of a runaway render, layered under the in-process `rlimits`. |
| Subprocess reaping | `init: true` | Reaps orphaned render children (PID 1 init). |

These were verified booting under the exact flags above: read-only rootfs
enforced, DB written to the mounted volume, and the scene-render subprocess
(fork + rlimits) working while injection payloads stay blocked.

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
| `RENDER_CONCURRENCY` | `1` | Max simultaneous render subprocesses (see memory budget below). |

> **Memory budget — keep these consistent.** gunicorn runs `--threads 4`, so up
> to 4 requests can render at once. Renders are serialized by `RENDER_CONCURRENCY`
> (default 1), so the worst-case render footprint is a single `RENDER_MEM_MB`.
> Size the container so `mem_limit ≥ RENDER_MEM_MB × RENDER_CONCURRENCY + ~512 MiB`
> headroom (web process + in-flight refs). With the defaults that's
> `1024 × 1 + 512 → 1536m`. This guarantees the per-render `RLIMIT_AS` returns a
> controlled "exceeded memory limit" error *before* the container OOM-killer
> restarts the web process. If you raise `RENDER_CONCURRENCY` or `RENDER_MEM_MB`,
> raise `mem_limit` to match.

> **Scaling note:** the image runs **one** gunicorn worker (with threads) on
> purpose — the rate-limit and form-token state is in-process. Running multiple
> workers/replicas needs a shared store (Redis); see the README.

## Roadmap: full network/syscall isolation for the render path (issue #2)

The current setup denies network/filesystem access to scene code at the
**language** level (no `import` ⇒ no `socket`) and caps resources, but the web
container still needs sockets itself (to serve HTTP and call the Anthropic API),
so it can't seccomp-block networking outright.

To close that gap, split rendering into a dedicated **network-less worker**:

1. A small service that only runs `renderer.render()`, reachable from the web
   tier over a Unix socket / shared volume (not the network).
2. Run that worker with `--network none` and the deny-by-default seccomp profile
   in [`seccomp/render-worker.json`](seccomp/render-worker.json) (networking
   syscalls omitted). **Test it with `strace` under your kernel/arch first** —
   it's a curated allowlist and may need a syscall or two added.
3. Give the worker its own tight CPU/memory/PID limits and a read-only rootfs.

Tracked in **issue #2**. Not required for the current single-instance, low-/no-
traffic deployment, but it's the prerequisite before this takes meaningful
untrusted third-party traffic.
