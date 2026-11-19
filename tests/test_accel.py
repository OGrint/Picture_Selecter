"""Tests for the optional graphics-chip acceleration (reviewer_accel)."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_accel as acc  # noqa: E402

try:
    import onnx
    from onnx import TensorProto, helper, numpy_helper
except Exception:          # onnx not installed: conversion tests are skipped
    onnx = None


def _tiny_model(path: Path) -> None:
    """Conv (patch embedding) → flatten → MatMul, like the start of a ViT."""
    rng = np.random.default_rng(0)
    w_conv = rng.standard_normal((8, 3, 4, 4)).astype(np.float32) * 0.2
    w_mm = rng.standard_normal((8 * 4 * 4, 16)).astype(np.float32) * 0.2
    g = helper.make_graph(
        [helper.make_node("Conv", ["image", "wc"], ["c"], strides=[4, 4]),
         helper.make_node("Flatten", ["c"], ["f"]),
         helper.make_node("MatMul", ["f", "wm"], ["out"])],
        "tiny",
        [helper.make_tensor_value_info("image", TensorProto.FLOAT, ["n", 3, 16, 16])],
        [helper.make_tensor_value_info("out", TensorProto.FLOAT, ["n", 16])],
        [numpy_helper.from_array(w_conv, "wc"), numpy_helper.from_array(w_mm, "wm")])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)])
    m.ir_version = 8
    onnx.save(m, str(path))


def _int8_model(path: Path):
    """
    The same two layers as onnxruntime's dynamic int8 quantisation writes them:
    DynamicQuantizeLinear → ConvInteger / MatMulInteger → Cast → Mul(scales).
    Returns the float weights the int8 ones stand for.
    """
    rng = np.random.default_rng(0)
    qc = rng.integers(-127, 128, (8, 3, 4, 4)).astype(np.int8)
    sc = (rng.uniform(0.5, 1.5, 8) * 0.002).astype(np.float32)        # per output channel
    qm = rng.integers(-127, 128, (128, 16)).astype(np.int8)
    sm = np.float32(0.003)
    T = numpy_helper.from_array
    nodes = [
        helper.make_node("DynamicQuantizeLinear", ["image"], ["xq", "xs", "xz"]),
        helper.make_node("ConvInteger", ["xq", "qc", "xz", "zc"], ["ci"], strides=[4, 4]),
        helper.make_node("Cast", ["ci"], ["cf"], to=TensorProto.FLOAT),
        helper.make_node("Mul", ["xs", "sc"], ["cs"]),
        helper.make_node("Mul", ["cf", "cs"], ["c"]),
        helper.make_node("Flatten", ["c"], ["f"]),
        helper.make_node("DynamicQuantizeLinear", ["f"], ["fq", "fs", "fz"]),
        helper.make_node("MatMulInteger", ["fq", "qm", "fz", "zm"], ["mi"]),
        helper.make_node("Cast", ["mi"], ["mf"], to=TensorProto.FLOAT),
        helper.make_node("Mul", ["fs", "sm"], ["ms"]),
        helper.make_node("Mul", ["mf", "ms"], ["out"]),
    ]
    g = helper.make_graph(
        nodes, "tiny_int8",
        [helper.make_tensor_value_info("image", TensorProto.FLOAT, ["n", 3, 16, 16])],
        [helper.make_tensor_value_info("out", TensorProto.FLOAT, ["n", 16])],
        [T(qc, "qc"), T(np.int8(0), "zc"), T(sc.reshape(8, 1, 1), "sc"),
         T(qm, "qm"), T(np.int8(0), "zm"), T(sm, "sm")])
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)])
    m.ir_version = 8
    onnx.save(m, str(path))
    return qc.astype(np.float32) * sc.reshape(8, 1, 1, 1), qm.astype(np.float32) * sm


@unittest.skipIf(onnx is None, "onnx not installed")
class DequantizeTests(unittest.TestCase):
    def test_int8_layers_become_float_and_match(self):
        import onnxruntime as ort
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            wc, wm = _int8_model(d / "int8.onnx")

            n = acc.dequantize_model(d / "int8.onnx", d / "float.onnx")
            self.assertEqual(n, 2)
            ops = {k.op_type for k in onnx.load(str(d / "float.onnx")).graph.node}
            self.assertFalse(ops & {"ConvInteger", "MatMulInteger", "DynamicQuantizeLinear"}, ops)

            x = np.random.default_rng(1).standard_normal((2, 3, 16, 16)).astype(np.float32)
            run = lambda p: ort.InferenceSession(str(p), providers=["CPUExecutionProvider"]) \
                .run(None, {"image": x})[0]
            got = run(d / "float.onnx")
            # what the int8 weights stand for, in plain numpy (stride-4 4x4 conv = patches)
            p = x.reshape(2, 3, 4, 4, 4, 4).transpose(0, 2, 4, 1, 3, 5).reshape(2, 16, 48)
            conv = (p @ wc.reshape(8, 48).T).transpose(0, 2, 1).reshape(2, 128)
            ref = conv @ wm
            self.assertGreater(acc._cosine(got, ref), 0.9999)
            # and the original int8 model agrees closely too
            self.assertGreater(acc._cosine(run(d / "int8.onnx"), ref), 0.99)


@unittest.skipIf(onnx is None, "onnx not installed")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "f32.onnx"
        _tiny_model(self.path)
        self.x = np.random.default_rng(2).standard_normal((1, 3, 16, 16)).astype(np.float32)

    def tearDown(self):
        acc._runners.clear()
        self.dir.cleanup()

    def test_cpu_when_turned_off(self):
        acc.configure(False, Path(self.dir.name))
        r = acc.Runner(self.path, 1, "test")
        self.assertEqual((r.device, r.state), ("CPU", "cpu"))
        self.assertEqual(r.run(None, {"image": self.x})[0].shape, (1, 16))
        self.assertEqual(acc.status(), "")

    def test_different_result_is_rejected(self):
        acc.configure(False, Path(self.dir.name))
        r = acc.Runner(self.path, 1, "test")

        class FakeCore:                                   # a "graphics chip" that is wrong
            def read_model(self, p):
                return __import__("openvino").Core().read_model(p)

            def compile_model(self, m, dev, cfg):
                return __import__("openvino").Core().compile_model(m, "CPU")

        orig_core, orig_gpu = acc._ov_core, acc.Runner._run_gpu
        acc._ov_core = lambda: FakeCore()
        acc.Runner._run_gpu = lambda self, req, x: -orig_gpu(self, req, x)
        try:
            r._prepare()
        finally:
            acc._ov_core, acc.Runner._run_gpu = orig_core, orig_gpu
        self.assertEqual(r.device, "CPU")
        self.assertIn("differs", r.note)

    def test_gpu_error_falls_back_to_cpu(self):
        acc.configure(False, Path(self.dir.name))
        r = acc.Runner(self.path, 1, "test")
        ref = r.run(None, {"image": self.x})[0]

        class Broken:
            def infer(self, _):
                raise RuntimeError("device lost")
        r._gpu, r.device, r._cpu = Broken(), "GPU", None
        got = r.run(None, {"image": self.x})[0]
        np.testing.assert_allclose(got, ref, rtol=1e-5)
        self.assertEqual(r.device, "CPU")


def _gpu_present() -> bool:
    try:
        import openvino as ov
        return "GPU" in ov.Core().available_devices
    except Exception:
        return False


@unittest.skipUnless(onnx is not None and _gpu_present(), "no graphics chip / OpenVINO")
class LiveGpuTests(unittest.TestCase):
    def test_graphics_chip_matches_cpu(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f32.onnx"
            _tiny_model(p)
            acc.configure(True, Path(d))
            acc.MIN_SPEEDUP, old = 0.0, acc.MIN_SPEEDUP        # tiny model: don't require speed
            try:
                r = acc.Runner(p, 1, "test")
                acc.wait_until_ready(120)
            finally:
                acc.MIN_SPEEDUP = old
            self.assertEqual(r.device, "GPU", r.note)
            x = np.random.default_rng(3).standard_normal((3, 3, 16, 16)).astype(np.float32)
            got = r.run(None, {"image": x})[0]                  # batch of 3 → one at a time
            ref = acc._ort_session(p, 1).run(None, {"image": x})[0]
            self.assertEqual(got.shape, (3, 16))
            self.assertGreater(acc._cosine(got, ref), 0.99)
            acc._runners.clear()
            acc.configure(False, None)


if __name__ == "__main__":
    unittest.main()
