"""Store large float32 weights as float16 (+ a Cast back to float32): half the file,
same arithmetic. usage: halve_weights.py <src.onnx> <dst.onnx>"""
import sys

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

src, dst = sys.argv[1:3]
m = onnx.load(src)
g = m.graph
casts, n = [], 0
for init in list(g.initializer):
    if init.data_type != TensorProto.FLOAT:
        continue
    a = numpy_helper.to_array(init)
    if a.size < 4096:
        continue
    half = numpy_helper.from_array(a.astype(np.float16), init.name + "__f16")
    g.initializer.remove(init)
    g.initializer.append(half)
    casts.append(helper.make_node("Cast", [half.name], [init.name], to=TensorProto.FLOAT,
                                  name=init.name + "__tofloat"))
    n += 1
nodes = casts + list(g.node)
del g.node[:]
g.node.extend(nodes)
onnx.save(m, dst)
print("halved", n, "weights to", dst)
