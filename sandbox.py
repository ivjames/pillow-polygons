"""
sandbox.py — scene-code sandbox, part B: run renderer.render() in a locked-down
subprocess.

renderer.py (part A) already restricts *what* scene code can do (no imports,
no dunders, no dangerous builtins). This module restricts *what it can consume*
and isolates crashes from the web process:

  - CPU time, address space (memory), and output file size are capped with
    POSIX rlimits set in the child before any scene code runs.
  - A wall-clock timeout kills a child that hangs (e.g. an infinite loop that
    somehow stays under the CPU limit, or blocks).
  - A segfault / OOM-kill / SIGXCPU in the child can't take down Flask — the
    parent just sees a failed render and returns a 400/500.

Because part A forbids `import`, the child can't reach the `socket` module, so
network access is already denied at the language level. Hard kernel-level
network/syscall isolation (seccomp / network namespace) needs root or a
container and is a deployment concern.
  # TODO: for defense in depth in production, also run this child inside a
  #       container/netns with seccomp and a read-only root FS.

All limits are env-tunable; defaults are generous enough for legitimate scenes.
"""

import io
import os
import multiprocessing as mp


def _cpu_seconds():  return int(os.environ.get("RENDER_CPU_SECONDS", 10))
def _mem_mb():       return int(os.environ.get("RENDER_MEM_MB", 1024))
def _wall_seconds(): return int(os.environ.get("RENDER_WALL_SECONDS", 20))
def _fsize_mb():     return int(os.environ.get("RENDER_FSIZE_MB", 64))


class RenderTimeout(Exception):
    """Render exceeded the wall-clock budget and was killed."""


class RenderError(Exception):
    """Render failed (crash, OOM, or an exception inside the scene code)."""


def _apply_rlimits():
    """Set resource limits in the child. Best-effort: a platform that lacks a
    given limit just skips it rather than failing the whole render."""
    import resource
    cpu = _cpu_seconds()
    for soft_hard, what in [
        ((cpu, cpu + 1),                               resource.RLIMIT_CPU),
        ((_mem_mb() * 1024 * 1024,) * 2,               getattr(resource, "RLIMIT_AS", None)),
        ((_fsize_mb() * 1024 * 1024,) * 2,             resource.RLIMIT_FSIZE),
    ]:
        if what is None:
            continue
        try:
            resource.setrlimit(what, soft_hard)
        except (ValueError, OSError):
            pass


def _child(q, scene_code, kwargs, ref_png):
    # Import inside the child so rlimits are applied before heavy work.
    from renderer import render as _render, SceneValidationError
    try:
        _apply_rlimits()
        ref = None
        if ref_png:
            from PIL import Image
            ref = Image.open(io.BytesIO(ref_png)).convert("RGB")
        result = _render(scene_code, ref=ref, **kwargs)
        q.put(("ok", result))
    except SceneValidationError as e:
        q.put(("blocked", str(e)))
    except MemoryError:
        q.put(("err", "render exceeded its memory limit"))
    except Exception as e:                       # noqa: BLE001 — report any failure to the parent
        q.put(("err", f"{type(e).__name__}: {e}"))


def run_scene(scene_code, ref=None, **kwargs):
    """Run renderer.render(scene_code, ref=ref, **kwargs) in a sandboxed child.

    Returns the render result dict. Raises:
      - renderer.SceneValidationError  if the scene used a forbidden construct,
      - RenderTimeout                  if it blew the wall-clock budget,
      - RenderError                    on crash/OOM/scene exception.
    """
    from renderer import SceneValidationError

    ref_png = None
    if ref is not None:
        buf = io.BytesIO()
        ref.convert("RGB").save(buf, format="PNG")
        ref_png = buf.getvalue()

    # fork keeps startup cheap and inherits the already-imported PIL/renderer.
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    proc = ctx.Process(target=_child, args=(q, scene_code, kwargs, ref_png), daemon=True)
    proc.start()
    proc.join(_wall_seconds())

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join()
        raise RenderTimeout(f"render exceeded {_wall_seconds()}s and was killed")

    try:
        status, payload = q.get(timeout=2)
    except Exception:
        # No message => the child died hard (segfault, SIGKILL from OOM/SIGXCPU).
        raise RenderError(f"render process died (exit code {proc.exitcode})")

    if status == "ok":
        return payload
    if status == "blocked":
        raise SceneValidationError(payload)
    raise RenderError(payload)
