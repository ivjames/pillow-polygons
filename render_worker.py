"""
render_worker.py — the network-less render worker (issue #2).

Runs as its own process/container with `--network none`, a seccomp profile, a
read-only root filesystem, and dropped capabilities. It is the ONLY place
Claude-generated scene code executes, so even a sandbox escape has no network
and no access to the web tier or its secrets.

It watches the shared job queue (see jobqueue.py), claims each job, renders it
via sandbox.run_scene() — which adds the in-process AST allowlist, restricted
builtins, subprocess rlimits, and wall-clock timeout — and writes the result
back. Start with:  python render_worker.py
"""

import os
import io
import sys
import json
import glob
import time

import sandbox
import jobqueue
from jobqueue import incoming, done, ensure_dirs, _atomic_write, _quiet_remove, started_marker
from renderer import SceneValidationError

# Default output dir matches the web image's RENDERS_DIR; both containers mount
# the same renders volume there, so basenames line up for serving.
RENDERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "renders")
IDLE_SLEEP = 0.05


def _load_ref(ref_name):
    if not ref_name:
        return None
    path = os.path.join(incoming(), ref_name)
    if not os.path.exists(path):
        return None
    from PIL import Image
    with open(path, "rb") as f:
        return Image.open(io.BytesIO(f.read())).convert("RGB")


def _result_for(spec):
    """Render one job spec; return a JSON-serializable result envelope."""
    try:
        scene_format = spec.get("scene_format", "python")
        ref = _load_ref(spec.get("ref")) if scene_format != "json" else None
        result = sandbox.run_scene(
            spec["scene_code"],
            filename=spec["filename"],
            width=spec["width"], height=spec["height"], seed=spec["seed"],
            ref=ref, preset=spec.get("preset"), scene_format=scene_format,
            thumbnail=spec.get("thumbnail", True),
            _output_dir=spec.get("output_dir") or RENDERS_DIR,
        )
        return {"status": "ok", "result": result}
    except SceneValidationError as e:
        return {"status": "blocked", "error": str(e)}
    except sandbox.RenderTimeout as e:
        return {"status": "timeout", "error": str(e)}
    except sandbox.RenderError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:                       # noqa: BLE001 — never let the loop die
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _process(spec_path):
    job_id = os.path.splitext(os.path.basename(spec_path))[0]
    # Claim the job by renaming so a second worker (or a retry) won't double-run it.
    claim_path = spec_path + ".claim"
    try:
        os.replace(spec_path, claim_path)
    except OSError:
        return  # someone else claimed it
    try:
        with open(claim_path) as f:
            spec = json.load(f)
    except Exception as e:
        _atomic_write(os.path.join(done(), f"{job_id}.json"),
                      json.dumps({"status": "error", "error": f"bad job spec: {e}"}))
        _quiet_remove(claim_path)
        return

    # Tell the web side the render is now starting, so its render-budget clock
    # begins here rather than at enqueue (queue wait doesn't count against it).
    jobqueue.touch(started_marker(job_id))

    envelope = _result_for(spec)
    _atomic_write(os.path.join(done(), f"{job_id}.json"), json.dumps(envelope))

    # Clean up the claimed spec + any ref; the web side removes the done/started files.
    _quiet_remove(claim_path)
    if spec.get("ref"):
        _quiet_remove(os.path.join(incoming(), spec["ref"]))


def main():
    ensure_dirs()
    hb_path = jobqueue.heartbeat_path()
    last_hb = 0.0
    print(f"render_worker: watching {incoming()} -> {done()}", flush=True)
    while True:
        specs = sorted(glob.glob(os.path.join(incoming(), "*.json")))
        if not specs:
            now = time.time()
            if now - last_hb > 2:        # liveness signal while idle
                jobqueue.touch(hb_path); last_hb = now
            time.sleep(IDLE_SLEEP)
            continue
        for spec_path in specs:
            jobqueue.touch(hb_path); last_hb = time.time()   # progress before each job
            _process(spec_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
