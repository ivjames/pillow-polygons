"""
jobqueue.py — filesystem hand-off between the web tier and the network-less
render worker (issue #2).

The web container can reach the network (it serves HTTP and calls the Anthropic
API), so scene code must not execute there. Instead the web tier writes a render
job to a shared volume and waits; a separate `render_worker.py` process — running
in a `--network none`, seccomp-confined, read-only container — picks the job up,
renders it, and writes the result back. No sockets, so the worker needs no
network stack at all.

Protocol (all under JOBS_DIR, default /jobs):
  incoming/<id>.ref.png   optional reference image (written first)
  incoming/<id>.json      job spec (written last, atomically — this is the trigger)
  done/<id>.json          result (written atomically by the worker)

Writes are tmp-file + os.replace so a reader never sees a half-written file.
This module is import-safe and has no side effects.
"""

import os
import io
import json
import time
import uuid

import sandbox                       # RenderTimeout / RenderError live here
from renderer import SceneValidationError


def jobs_dir():  return os.environ.get("JOBS_DIR", "/jobs")
def incoming():  return os.path.join(jobs_dir(), "incoming")
def done():      return os.path.join(jobs_dir(), "done")

# The worker writes a per-job "started" marker when it begins rendering, and
# refreshes a global heartbeat as it works. The web tier uses these so a job that
# is merely *queued* behind others isn't timed out against one render's budget.
def started_marker(job_id):  return os.path.join(done(), f"{job_id}.started")
def heartbeat_path():        return os.path.join(jobs_dir(), ".heartbeat")

POLL_INTERVAL = 0.05

# Budget for a single render once the worker has actually started it. The worker
# enforces its own wall-clock limit (RENDER_WALL_SECONDS); we wait a bit longer
# so its own timeout/error reaches us as a result rather than racing it.
def _render_budget():
    return int(os.environ.get("RENDER_WALL_SECONDS", 20)) + 15

# While a job is still queued, we keep waiting as long as the worker shows signs
# of life. It can't refresh the heartbeat while blocked in another job's render,
# so the liveness window must exceed one render's wall time.
def _worker_liveness():
    return int(os.environ.get("RENDER_WALL_SECONDS", 20)) + 20

# Absolute backstop so a wedged queue can't hang a request forever.
def _max_total_wait():
    return int(os.environ.get("RENDER_QUEUE_MAX_WAIT", 300))


def touch(path):
    """Write the current time to a marker file (no fsync — cheap progress signal)."""
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def ensure_dirs():
    for d in (incoming(), done()):
        os.makedirs(d, exist_ok=True)


def _atomic_write(path, data, mode="w"):
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, mode) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _quiet_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def submit_and_wait(scene_code, *, filename, width, height, seed,
                    ref=None, thumbnail=True, output_dir=None,
                    scene_format="python"):
    """Hand a render job to the worker and block for its result.

    Returns the same dict renderer.render() produces. Raises the same exceptions
    as sandbox.run_scene() so app.py treats both render paths identically:
      SceneValidationError / sandbox.RenderTimeout / sandbox.RenderError.
    """
    ensure_dirs()
    job_id = uuid.uuid4().hex

    ref_name = None
    if ref is not None:
        ref_name = f"{job_id}.ref.png"
        buf = io.BytesIO()
        ref.convert("RGB").save(buf, format="PNG")
        _atomic_write(os.path.join(incoming(), ref_name), buf.getvalue(), mode="wb")

    spec = {
        "id": job_id, "scene_code": scene_code, "filename": filename,
        "width": width, "height": height, "seed": seed,
        "thumbnail": thumbnail, "ref": ref_name, "output_dir": output_dir,
        "scene_format": scene_format,
    }
    spec_path = os.path.join(incoming(), f"{job_id}.json")
    _atomic_write(spec_path, json.dumps(spec))

    done_path     = os.path.join(done(), f"{job_id}.json")
    started_path  = started_marker(job_id)
    hb_path       = heartbeat_path()
    submit_time   = time.time()
    hard_deadline = submit_time + _max_total_wait()
    render_deadline = None        # set once the worker starts THIS job
    try:
        while True:
            if os.path.exists(done_path):
                with open(done_path) as f:
                    res = json.load(f)
                return _interpret(res)

            now = time.time()

            # Once the worker marks this job started, the render clock begins —
            # queue wait before that point doesn't count against the render budget.
            if render_deadline is None and os.path.exists(started_path):
                try:
                    render_deadline = os.path.getmtime(started_path) + _render_budget()
                except OSError:
                    render_deadline = now + _render_budget()

            if render_deadline is not None:
                if now > render_deadline:
                    raise sandbox.RenderTimeout(
                        f"render exceeded {_render_budget()}s after it started")
            else:
                # Still queued. Keep waiting only while the worker is alive —
                # the heartbeat must have advanced within the liveness window.
                try:
                    last_progress = os.path.getmtime(hb_path)
                except OSError:
                    last_progress = submit_time   # worker hasn't beat yet
                if now - last_progress > _worker_liveness():
                    raise sandbox.RenderError(
                        "render worker is not responding (stale heartbeat)")

            if now > hard_deadline:
                raise sandbox.RenderTimeout(
                    f"render did not complete within {_max_total_wait()}s")

            time.sleep(POLL_INTERVAL)
    finally:
        # Best-effort cleanup; the worker removes the incoming files when it
        # claims a job, but if it never ran we clean up our own.
        _quiet_remove(done_path)
        _quiet_remove(started_path)
        _quiet_remove(spec_path)
        if ref_name:
            _quiet_remove(os.path.join(incoming(), ref_name))


def _interpret(res):
    status = res.get("status")
    if status == "ok":
        return res["result"]
    if status == "blocked":
        raise SceneValidationError(res.get("error", "scene rejected by sandbox"))
    if status == "timeout":
        raise sandbox.RenderTimeout(res.get("error", "render timed out"))
    raise sandbox.RenderError(res.get("error", "render failed"))
