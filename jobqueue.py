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

POLL_INTERVAL = 0.05

# How long the web tier waits for a result. The worker enforces its own
# per-render wall-clock limit (RENDER_WALL_SECONDS); we wait a bit longer so the
# worker's own timeout/error reaches us as a result rather than racing it.
def _result_timeout():
    return int(os.environ.get("RENDER_WALL_SECONDS", 20)) + 15


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
                    ref=None, preset=None, thumbnail=True, output_dir=None):
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
        "width": width, "height": height, "seed": seed, "preset": preset,
        "thumbnail": thumbnail, "ref": ref_name, "output_dir": output_dir,
    }
    spec_path = os.path.join(incoming(), f"{job_id}.json")
    _atomic_write(spec_path, json.dumps(spec))

    done_path = os.path.join(done(), f"{job_id}.json")
    deadline = time.time() + _result_timeout()
    try:
        while time.time() < deadline:
            if os.path.exists(done_path):
                with open(done_path) as f:
                    res = json.load(f)
                return _interpret(res)
            time.sleep(POLL_INTERVAL)
        raise sandbox.RenderTimeout(
            f"render worker did not respond within {_result_timeout()}s")
    finally:
        # Best-effort cleanup; the worker removes the incoming files when it
        # claims a job, but if it never ran we clean up our own.
        _quiet_remove(done_path)
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
