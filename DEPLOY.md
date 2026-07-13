# Deploying Pillow Polygons

There are two supported ways to run this app, differing **only** in how much
they isolate the step that executes model-generated scene code:

- **Single process under pm2 (`RENDER_MODE=local`)** — how it runs on the
  lab980 droplet today. One Flask process; scene code executes in an in-process
  **subprocess sandbox** (AST allowlist + restricted builtins + subprocess
  rlimits). No Docker. Simplest to operate.
- **Two containers under Docker (`RENDER_MODE=worker`)** — the hardened setup.
  Scene code runs only in a separate, **network-less** worker container
  (`network_mode: none` + seccomp), so a sandbox escape has no network and no
  API key. Stronger isolation; needs Docker.

Both share the same application-level sandbox in the code (see the README
"Security" section). What differs is the render path's blast radius if that
sandbox is ever escaped — see the isolation notes below.

---

## How it runs on lab980 (pm2, no Docker)

Deployed at `/var/www/poly`, run by pm2 as **`poly-app`**: `python3 app.py`,
listening on `127.0.0.1:8040` behind an nginx TLS proxy. `RENDER_MODE` is unset,
so it uses the default `local` (the in-process subprocess sandbox).

### Deploy / redeploy

```bash
cd /var/www/poly
git pull
python3 -m venv .venv && . .venv/bin/activate    # first time only
pip install -r requirements.txt
# .env holds ANTHROPIC_API_KEY (the app loads it); the app listens on 8040
pm2 restart poly-app
# first time instead:
#   pm2 start app.py --name poly-app --interpreter python3
pm2 save                     # snapshot the current process list to the dump
```

**One time per droplet — install pm2's boot hook, or `poly-app` won't come back
after a reboot.** `pm2 save` only writes the dump; without the systemd hook
nothing replays it at boot.

```bash
pm2 startup systemd -u root --hp /root   # run the sudo command it prints, once
systemctl is-enabled pm2-root            # verify -> should print `enabled`
```

Verify:

```bash
curl -s http://127.0.0.1:8040/api/tags   # -> [] (200)
pm2 logs poly-app --lines 50
```

> **Isolation in `local` mode.** Scene code runs in a subprocess behind the AST
> allowlist and rlimits, but that subprocess shares this process's network
> namespace — so a hypothetical sandbox escape *could* reach the network and the
> Anthropic API key. That residual gap is the whole reason the `worker` mode
> below exists. `local` is a reasonable trade-off for a single-user portfolio
> deploy; if you need the network-less guarantee, run the Docker worker setup.

> **Heads up: `deploy.sh` is stale.** It writes an older `app.py` to
> `/var/www/poly` via heredoc that predates the `RENDER_MODE`/`SCENE_FORMAT`
> sandbox split. Deploy with `git pull` + `pm2 restart` as above — don't run
> `deploy.sh`.

### nginx TLS proxy

A certbot-managed vhost proxies the subdomain to the local port:

```
server_name poly.lab980.com;
location / { proxy_pass http://127.0.0.1:8040; }
```

Set `TRUST_PROXY=1` so the per-IP rate limits read the real client IP from
`X-Forwarded-For` instead of the proxy's address.

---

## Hardened alternative: the network-less worker (Docker, `RENDER_MODE=worker`)

A two-container setup that layers **network isolation** and **container**
isolation on top of the application-level scene-code sandbox. Not what the
droplet runs today, but the stronger posture if this ever took real traffic.

### Architecture — the render path has no network

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

### Quick start

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

### What the container hardening adds

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

### seccomp portability

`seccomp/render-worker.json` is a deny-by-default allowlist curated and verified
on CPython 3.12 + Pillow + multiprocessing-fork on x86_64/glibc. It is
kernel/glibc/arch-sensitive. If a render fails on your platform, `strace` the
worker for the `EPERM` syscall and add it — or drop the `seccomp:...` line from
the `render` service to fall back to `--network none` + Docker's default seccomp,
which still fully isolates the render path from the network.

### Reverse proxy + real client IPs

The container publishes to `127.0.0.1` only. Terminate TLS with a proxy
(Caddy/nginx/Traefik) in front, and set `TRUST_PROXY=1` (already set in compose)
so the per-IP rate limits read the real client IP from `X-Forwarded-For` instead
of the proxy's address. Example Caddy:

```
polygons.example.com {
    reverse_proxy 127.0.0.1:8040
}
```

### Memory & scaling

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

---

## Environment variables

`ANTHROPIC_API_KEY` is required (via `.env`). All anti-abuse and sandbox knobs
are documented in the README; the deployment-relevant additions are:

| Var | Default | Meaning |
|-----|---------|---------|
| `POLY_DB_PATH` | `<app>/poly.db` | SQLite location — pointed at the `/data` volume so the rootfs can stay read-only. |
| `RENDER_CPU_SECONDS` / `RENDER_MEM_MB` / `RENDER_WALL_SECONDS` / `RENDER_FSIZE_MB` | `10` / `1024` / `20` / `64` | Per-render subprocess limits. |
| `RENDER_CONCURRENCY` | `1` | Max simultaneous render subprocesses in the worker (see memory budget above). |
| `RENDER_MODE` | `local` | `worker` routes renders to the network-less worker (compose sets this on `web`); `local` runs the in-process sandbox (the lab980 default). |
| `SCENE_FORMAT` | `python` | `json` makes the model emit a declarative JSON scene drawn by a fixed interpreter (no code execution; see the README "Scene format" section). `python` is the default exec'd-scene-code path. |
| `JOBS_DIR` | `/jobs` | Shared job-queue directory (mounted into both `web` and `render`) — worker mode only. |
| `RENDER_QUEUE_MAX_WAIT` | `300` | Absolute backstop (seconds) the web tier waits for a queued render before giving up. A job only counts against the per-render budget once the worker *starts* it; queue wait is bounded by this instead. |

## Network/syscall isolation of the render path (issue #2)

In `worker` mode, scene code executes **only** in the `render` service, which
runs with `network_mode: none` and the deny-by-default seccomp profile in
[`seccomp/render-worker.json`](seccomp/render-worker.json) (networking syscalls
omitted). The web tier never `exec()`s scene code; it hands jobs over the shared
`/jobs` queue and waits. This closes the residual gap present in `local` mode,
where the render subprocess inherits the web process's network namespace. See
the architecture diagram above and the seccomp portability note for tuning on
other kernels/arches.
