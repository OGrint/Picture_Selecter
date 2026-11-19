"""Export MobileNetV3-Large (ImageNet) as a 960-d image-embedding ONNX model."""
import sys
import numpy as np
import torch
import torchvision

out_path = sys.argv[1]
m = torchvision.models.mobilenet_v3_large(weights=torchvision.models.MobileNet_V3_Large_Weights.IMAGENET1K_V2)
m.eval()
net = torch.nn.Sequential(m.features, m.avgpool, torch.nn.Flatten()).eval()
dummy = torch.zeros(1, 3, 224, 224)
torch.onnx.export(net, dummy, out_path, opset_version=17, input_names=["image"],
                  output_names=["embedding"], dynamic_axes={"image": {0: "n"}, "embedding": {0: "n"}},
                  dynamo=False)
import onnxruntime as ort  # noqa: E402
x = np.random.rand(2, 3, 224, 224).astype(np.float32)
with torch.no_grad():
    t = net.eval()(torch.from_numpy(x)).numpy()
o = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"]).run(None, {"image": x})[0]
print("shape", o.shape, "max abs diff", float(np.abs(o - t).max()))
