"""Bake the eye/head text queries into OWLv2: one input (pixel_values), one output
(1, patches, 4 + 4) = sigmoid scores for the 4 queries, then cx, cy, w, h.
usage: bake_eye.py <src.onnx> <dst.onnx>"""
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))      # the app folder (for the tokenizer)
from reviewer_search import ClipTokenizer, _find  # noqa: E402

QUERIES = ["an eye", "a bird's eye", "an animal's eye", "a head"]
src, dst = sys.argv[1:3]
tok = ClipTokenizer(_find("clip_bpe_vocab.txt.gz"))
ids = np.zeros((len(QUERIES), 16), np.int64)
att = np.zeros_like(ids)
for i, q in enumerate(QUERIES):
    t = [tok.sot] + tok.encode(q)[:14] + [tok.eot]
    ids[i, :len(t)] = t
    att[i, :len(t)] = 1

m = onnx.load(src)
g = m.graph
for name, arr in (("input_ids", ids), ("attention_mask", att)):
    inp = next((i for i in g.input if i.name == name), None)
    if inp is not None:
        g.input.remove(inp)
        g.initializer.append(numpy_helper.from_array(arr, name))
print("outputs:", [o.name for o in g.output])
g.node.extend([
    helper.make_node("Sigmoid", ["logits"], ["eye_scores"]),
    helper.make_node("Concat", ["eye_scores", "pred_boxes"], ["eye_out"], axis=-1),
])
for o in list(g.output):
    g.output.remove(o)
g.output.append(helper.make_tensor_value_info("eye_out", onnx.TensorProto.FLOAT, [1, None, 8]))
inp = next(i for i in g.input if i.name == "pixel_values")
for d, v in zip(inp.type.tensor_type.shape.dim, (1, 3, 960, 960)):
    d.ClearField("dim_param")
    d.dim_value = v
onnx.checker.check_model(m, full_check=False) if m.ByteSize() < 2_000_000_000 else None
onnx.save(m, dst)
print("saved", dst)
