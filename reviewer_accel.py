"""
Optional graphics-chip acceleration for Photo Reviewer's ONNX models.

Every model starts on the CPU (onnxruntime) straight away. If OpenVINO is
installed and the computer has a supported graphics chip (e.g. Intel Arc /
Iris / "Intel Graphics"), a background thread prepares a graphics-chip copy
of the model, checks it gives the same answer as the CPU and is faster, and
only then switches over. Anything unusual (no graphics chip, driver error,
different results, slower) silently keeps the CPU. Nothing here changes the
results stored in the per-folder caches.

The int8 models (species, search) can't run in half precision as they are,
so a float copy is made once (needs the "onnx" package) and kept in
%APPDATA%\\PhotoReviewer\\gpu_models.
"""

from __future__ import annotations

import atexit
import gc
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("photo_reviewer")

MIN_AGREEMENT = 0.95      # cosine similarity to the CPU output on the check input
MIN_SPEEDUP   = 1.15      # graphics chip must be at least this much faster

_enabled = os.environ.get("PHOTO_REVIEWER_GPU", "1") != "0"
_cache_dir: Optional[Path] = None
_device_name: Optional[str] = None      # e.g. "Intel(R) Graphics (iGPU)"
_unavailable: Optional[str] = None      # why the graphics chip isn't used
_runners: list["Runner"] = []
_jobs: "queue.Queue[Runner]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_lock = threading.Lock()
_core = None
# Shutting down cleanly matters: OpenVINO's graphics plugin can hang the
# process if its objects are still alive (or in use) when Windows unloads it.
_stopping = False
_active = 0                              # threads inside OpenVINO calls being set up
_active_cv = threading.Condition()
_helper = None                           # conversion helper process, if running


def configure(enabled: bool, cache_dir: Optional[Path]) -> None:
    """Call once at start-up (before models load)."""
    global _enabled, _cache_dir
    _enabled = bool(enabled) and os.environ.get("PHOTO_REVIEWER_GPU", "1") != "0"
    _cache_dir = cache_dir
    if _enabled:                           # look for the graphics chip without blocking
        threading.Thread(target=_busy(_ov_core), daemon=True, name="gpu-probe").start()


def _busy(fn):
    """Wrap fn so shutdown() waits for it to finish."""
    def run(*a):
        global _active
        with _active_cv:
            if _stopping:
                return None
            _active += 1
        try:
            return fn(*a)
        finally:
            with _active_cv:
                _active -= 1
                _active_cv.notify_all()
    return run


def shutdown(timeout: float = 20.0) -> None:
    """Release the graphics chip before the program exits (runs at exit)."""
    global _stopping, _core
    with _active_cv:
        _stopping = True
    h = _helper
    if h is not None and h.poll() is None:
        try:
            h.kill()
        except Exception:
            pass
    with _active_cv:                      # let a compile in progress finish
        _active_cv.wait_for(lambda: _active == 0, timeout)
    for r in _runners:
        r._gpu, r.device = None, "CPU"
    _core = None
    gc.collect()


atexit.register(shutdown)


def gpu_found() -> bool:
    """True once a usable graphics chip has been found (never blocks)."""
    return _enabled and _core is not None


def status() -> str:
    """One line for the UI: which models run on the graphics chip."""
    on = sorted({r.name for r in _runners if r.device == "GPU"})
    pending = any(r.state == "preparing" for r in _runners)
    if on:
        return "using graphics chip" + ("…" if pending else "")
    if pending:
        return "preparing graphics chip…"
    return ""


def detail() -> str:
    """Longer description (for logs / About)."""
    if not _enabled:
        return "Graphics acceleration: off"
    if _unavailable:
        return f"Graphics acceleration unavailable: {_unavailable}"
    lines = [f"Graphics chip: {_device_name or 'not checked yet'}"]
    for r in _runners:
        lines.append(f"  {r.name}: {r.device}" + (f" ({r.note})" if r.note else ""))
    return "\n".join(lines)


# ── Runner (drop-in for onnxruntime.InferenceSession.run) ────────────────────

class Runner:
    """
    run(None, {input: array}) → [output0], like onnxruntime. Starts on CPU;
    switches to the graphics chip when a verified copy is ready.
    """

    def __init__(self, path: Path, threads: int, name: str, gpu: bool = True,
                 dequantize: bool = False):
        self.path, self.threads, self.name = Path(path), threads, name
        self.dequantize = dequantize
        self.device, self.state, self.note = "CPU", "cpu", ""
        self._cpu = _ort_session(self.path, threads)
        inp = self._cpu.get_inputs()[0]
        self.input_name, self.input_shape = inp.name, list(inp.shape)
        self.input_type = np.int64 if "int64" in inp.type else np.float32
        self._gpu = None
        self._gpu_lock = threading.Lock()
        self._cpu_lock = threading.Lock()
        _runners.append(self)
        if gpu and _enabled and _unavailable is None:
            self.state = "preparing"
            _submit(self)

    def get_inputs(self):
        return self._cpu_session().get_inputs()

    def run(self, _names, feeds: dict) -> list:
        x = feeds[self.input_name]
        gpu = self._gpu
        if gpu is not None:
            try:
                return [self._run_gpu(gpu, x)]
            except Exception as e:                     # driver reset etc. → CPU for good
                log.warning("Graphics chip failed for %s (%s); using the CPU", self.name, e)
                self._gpu, self.device, self.note = None, "CPU", "graphics chip error"
        return self._cpu_session().run(None, {self.input_name: x})[:1]

    def _cpu_session(self):
        with self._cpu_lock:
            if self._cpu is None:
                self._cpu = _ort_session(self.path, self.threads)
            return self._cpu

    def _run_gpu(self, req, x: np.ndarray) -> np.ndarray:
        # compiled with batch 1; larger batches run one at a time
        with self._gpu_lock:
            if x.shape[0] == 1:
                return np.array(next(iter(req.infer({0: x}).values())))
            return np.concatenate([np.array(next(iter(req.infer({0: x[i:i + 1]}).values())))
                                   for i in range(x.shape[0])])

    def _static_shape(self) -> list[int]:
        dims = [d if isinstance(d, int) and d > 0 else None for d in self.input_shape]
        dims[0] = 1
        if self.input_type is np.int64:
            return [1 if d is None else d for d in dims]
        return [d if d is not None else 224 for d in dims]

    def _check_input(self, shape) -> np.ndarray:
        rng = np.random.default_rng(1234)
        if self.input_type is np.int64:                  # CLIP text tokens
            x = np.zeros(shape, np.int64)
            x[0, :6] = [49406, 320, 1125, 539, 320, 49407]
            return x
        # smooth image-like pattern plus noise, roughly normalised range
        h, w = shape[2], shape[3]
        yy, xx = np.mgrid[0:h, 0:w] / max(h, w)
        base = np.stack([np.sin(6 * xx), np.cos(5 * yy), np.sin(4 * (xx + yy))])
        x = 0.6 * base[None] + 0.3 * rng.standard_normal(shape)
        if self.name == "animal detection":             # this model takes 0–1 pixels
            x = 0.5 + 0.4 * x
        return x.astype(np.float32)

    def _prepare(self) -> None:
        """Background: build, check and switch to the graphics-chip copy."""
        try:
            core = _ov_core()
            if core is None:
                self.state = "cpu"
                return
            src = _gpu_model_file(self) if self.dequantize else self.path
            if src is None:
                self.state, self.note = "cpu", "needs the onnx package"
                return
            m = core.read_model(str(src))
            shape = self._static_shape()
            m.reshape({m.inputs[0].get_any_name(): shape})
            t0 = time.perf_counter()
            compiled = core.compile_model(m, "GPU", {"PERFORMANCE_HINT": "LATENCY",
                                                     "INFERENCE_PRECISION_HINT": "f16"})
            req = compiled.create_infer_request()
            compile_s = time.perf_counter() - t0
            if _stopping:
                self.state = "cpu"
                return
            x = self._check_input(shape)
            ref = self._cpu_session().run(None, {self.input_name: x})[0]
            got = self._run_gpu(req, x)
            agree = _cosine(got, ref)
            if not np.isfinite(got).all() or agree < MIN_AGREEMENT:
                self.state, self.note = "cpu", f"graphics chip result differs ({agree:.3f})"
                log.info("Graphics chip rejected for %s: agreement %.4f", self.name, agree)
                return
            t_gpu = _time(lambda: self._run_gpu(req, x))
            t_cpu = _time(lambda: self._cpu_session().run(None, {self.input_name: x}))
            if t_gpu * MIN_SPEEDUP > t_cpu:
                self.state, self.note = "cpu", "CPU is as fast"
                log.info("Graphics chip not faster for %s (%.0f vs %.0f ms)",
                         self.name, t_gpu * 1000, t_cpu * 1000)
                return
            if _stopping:
                self.state = "cpu"
                return
            self._gpu, self.device, self.state = req, "GPU", "gpu"
            self.note = f"{t_cpu / t_gpu:.1f}× faster"
            with self._cpu_lock:
                self._cpu = None                         # free its memory; reloaded if needed
            log.info("%s on the graphics chip: %.0f → %.0f ms (agreement %.4f, compile %.1fs)",
                     self.name, t_cpu * 1000, t_gpu * 1000, agree, compile_s)
        except Exception as e:
            self.state, self.note = "cpu", "graphics chip error"
            log.info("Graphics chip unavailable for %s: %s", self.name, e)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ort_session(path: Path, threads: int):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, threads)
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def _cosine(a, b) -> float:
    a, b = np.asarray(a, np.float64).ravel(), np.asarray(b, np.float64).ravel()
    if not np.isfinite(a).all():
        return 0.0
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def _time(fn: Callable, n: int = 5) -> float:
    fn()
    times = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return sorted(times)[n // 2]


def _ov_core():
    """OpenVINO core if a graphics chip is available, else None."""
    global _core, _unavailable, _device_name
    with _lock:
        if _core is not None or _unavailable is not None:
            return _core
        try:
            import openvino as ov
        except Exception:
            _unavailable = "OpenVINO not installed"
            return None
        try:
            core = ov.Core()
            if "GPU" not in core.available_devices:
                _unavailable = "no supported graphics chip"
                return None
            _device_name = core.get_property("GPU", "FULL_DEVICE_NAME")
            _core = core
            log.info("Graphics chip: %s", _device_name)
        except Exception as e:
            _unavailable = str(e)
        return _core


def _submit(r: Runner) -> None:
    global _worker
    _jobs.put(r)
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, daemon=True, name="gpu-prepare")
            _worker.start()


def _work() -> None:
    while True:
        r = _jobs.get()
        if _unavailable is None and not _stopping:
            _busy(r._prepare)()
        if r.state == "preparing":
            r.state = "cpu"


def _gpu_model_file(r: Runner) -> Optional[Path]:
    """Float copy of an int8 model (made once, then reused)."""
    base = _cache_dir or Path(os.environ.get("APPDATA", Path.home())) / "PhotoReviewer"
    folder = base / "gpu_models"
    st = r.path.stat()
    out = folder / f"{r.path.stem}.w8.{st.st_size}.{int(st.st_mtime)}.onnx"   # (w8: weight-only int8)
    if out.exists():
        return out
    try:
        import onnx  # noqa: F401
    except Exception:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{r.path.stem}.*.onnx"):      # older model versions
        try:
            old.unlink()
        except OSError:
            pass
    r.note = "making graphics-chip copy (one-off)…"
    tmp = out.with_suffix(".tmp")
    _dequantize_in_helper(r.path, tmp)
    os.replace(tmp, out)
    r.note = ""
    return out


DEQUANTIZE_FLAG = "--make-gpu-model"


def _dequantize_in_helper(src: Path, dst: Path) -> None:
    """
    Run dequantize_model in a separate process: it holds Python's lock for
    seconds at a time, which would freeze the window.
    """
    import subprocess
    global _helper
    if getattr(sys, "frozen", False):                    # PhotoReviewer.exe handles the flag
        cmd = [sys.executable, DEQUANTIZE_FLAG, str(src), str(dst)]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), DEQUANTIZE_FLAG, str(src), str(dst)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with _active_cv:
        if _stopping:
            raise RuntimeError("closing")
        _helper = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   text=True, creationflags=flags)
    try:
        _out, err = _helper.communicate(timeout=1800)
    except subprocess.TimeoutExpired:
        _helper.kill()
        raise RuntimeError("model conversion took too long")
    finally:
        code, _helper = _helper.returncode, None
    if code != 0 or not dst.exists():
        raise RuntimeError(f"model conversion failed: {(err or '').strip()[-300:]}")


def main(argv: list[str]) -> int:
    """Helper-process entry: <flag> <int8 model> <output>."""
    if len(argv) != 3 or argv[0] != DEQUANTIZE_FLAG:
        return 2
    dequantize_model(Path(argv[1]), Path(argv[2]))
    return 0


def dequantize_model(src: Path, dst: Path) -> int:
    """
    Rewrite onnxruntime dynamic-int8 layers (DynamicQuantizeLinear →
    MatMulInteger/ConvInteger → Cast → Mul) as float MatMul/Conv whose weights
    stay int8 behind a DequantizeLinear ("weight-only" int8). The graphics chip
    then works in fp16 — the int8 *activations* were what overflowed there —
    while OpenVINO keeps the weights compressed, so the copy is about the size of
    the original instead of twice it. Returns the number of layers rewritten.
    """
    import onnx
    from onnx import helper, numpy_helper

    m = onnx.load(str(src))
    g = m.graph
    init = {i.name: i for i in g.initializer}
    const = {n.output[0]: n for n in g.node if n.op_type == "Constant"}

    def val(name):
        if name in init:
            return numpy_helper.to_array(init[name])
        a = next(a for a in const[name].attribute if a.name == "value")
        return numpy_helper.to_array(a.t)

    prod = {o: n for n in g.node for o in n.output}
    cons: dict[str, list] = {}
    for n in g.node:
        for i in n.input:
            cons.setdefault(i, []).append(n)

    new_inits, drop, repl = [], set(), {}
    for n in list(g.node):
        if n.op_type not in ("MatMulInteger", "ConvInteger"):
            continue
        dql = prod[n.input[0]]
        cast = cons[n.output[0]][0]
        mul = cons[cast.output[0]][0]
        if dql.op_type != "DynamicQuantizeLinear" or cast.op_type != "Cast" or mul.op_type != "Mul":
            raise ValueError(f"unexpected int8 pattern at {n.name}")
        smul = prod[next(i for i in mul.input if i != cast.output[0])]
        ws = np.asarray(val(next(i for i in smul.input if i != dql.output[1])), np.float32)
        wq = np.asarray(val(n.input[1]))                       # int8 / uint8, kept as is
        if len(n.input) > 3 and n.input[3]:
            wz = np.asarray(val(n.input[3])).astype(wq.dtype)
        else:
            wz = np.zeros(ws.shape, wq.dtype)
        wn = f"{n.name or n.op_type}_{len(repl)}_w8"
        ws, wz = ws.reshape(-1) if ws.size > 1 else ws.reshape(()), \
            wz.reshape(-1) if wz.size > 1 else wz.reshape(())
        new_inits += [numpy_helper.from_array(wq, wn + "_q"),
                      numpy_helper.from_array(ws, wn + "_scale"),
                      numpy_helper.from_array(np.broadcast_to(wz, ws.shape).copy(), wn + "_zp")]
        extra = {}
        if ws.ndim == 1:                     # per channel: Conv out-channels, MatMul columns
            extra["axis"] = 0 if n.op_type == "ConvInteger" else 1
        to32 = helper.make_node("DequantizeLinear", [wn + "_q", wn + "_scale", wn + "_zp"],
                                [wn + "_f32"], name=wn + "_dq", **extra)
        if n.op_type == "MatMulInteger":
            op = helper.make_node("MatMul", [dql.input[0], wn + "_f32"], [mul.output[0]],
                                  name=wn + "_float")
        else:
            op = helper.make_node("Conv", [dql.input[0], wn + "_f32"], [mul.output[0]],
                                  name=wn + "_float",
                                  **{a.name: helper.get_attribute_value(a) for a in n.attribute})
        repl[id(mul)] = [to32, op]
        drop.update((id(n), id(cast)))

    nodes = []
    for k in g.node:
        if id(k) in repl:
            nodes.extend(repl[id(k)])
        elif id(k) not in drop:
            nodes.append(k)
    g.initializer.extend(new_inits)
    # drop everything no longer feeding an output (quantize steps, int8 weights)
    needed = {o.name for o in g.output}
    alive = []
    for k in reversed(nodes):
        if any(o in needed for o in k.output):
            alive.append(k)
            needed.update(k.input)
    del g.node[:]
    g.node.extend(reversed(alive))
    keep = [i for i in g.initializer if i.name in needed]
    del g.initializer[:]
    g.initializer.extend(keep)
    onnx.save(m, str(dst))
    return len(repl)


def wait_until_ready(timeout: float = 600) -> None:
    """Block until every pending graphics-chip preparation has finished (tests)."""
    end = time.time() + timeout
    while time.time() < end and any(r.state == "preparing" for r in _runners):
        time.sleep(0.2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
