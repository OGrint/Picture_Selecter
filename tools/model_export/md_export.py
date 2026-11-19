"""Export MegaDetector MDv1000-spruce to ONNX and check it matches PyTorch."""
import sys
import numpy as np
import torch

out_path = sys.argv[1]
from megadetector.detection.run_detector import load_detector  # noqa: E402

det = load_detector("MDV1000-SPRUCE")
model = det.model
model.eval()
print("detector:", type(det).__name__, "model:", type(model).__name__)
for m in model.modules():                      # YOLOv5 Detect: single tensor output
    if type(m).__name__ == "Detect" and hasattr(m, "export"):
        m.export = True
dummy = torch.zeros(1, 3, 640, 640)
with torch.no_grad():
    ref = model(dummy)
ref0 = ref[0] if isinstance(ref, (tuple, list)) else ref
print("torch output shape:", tuple(ref0.shape))
torch.onnx.export(model, dummy, out_path, opset_version=17, input_names=["images"],
                  output_names=["output"], dynamo=False, do_constant_folding=True)
print("exported", out_path)

# Equivalence on a real image
import onnxruntime as ort  # noqa: E402
from PIL import Image  # noqa: E402
img = Image.open(sys.argv[2]).convert("RGB")
img.thumbnail((640, 640))
canvas = Image.new("RGB", (640, 640), (114, 114, 114))
canvas.paste(img, ((640 - img.width) // 2, (640 - img.height) // 2))
x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
with torch.no_grad():
    t = model(torch.from_numpy(x))
t = (t[0] if isinstance(t, (tuple, list)) else t).numpy()
o = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"]).run(None, {"images": x})[0]
print("onnx output shape:", o.shape, "max abs diff vs torch:", float(np.abs(o - t).max()))
conf = (o[0, :, 4:5] * o[0, :, 5:]).max(1)
print("top confidences:", np.sort(conf)[::-1][:5].round(3))
print("megadetector reference:", det.generate_detections_one_image(np.asarray(Image.open(sys.argv[2]).convert("RGB")),
                                                                  "img", detection_threshold=0.1)["detections"][:5])
