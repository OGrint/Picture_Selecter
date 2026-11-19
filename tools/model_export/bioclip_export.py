"""Export BioCLIP 2 image encoder to ONNX (+ int8), subset species embeddings, verify."""
import glob
import json
import os
import sys
import time

import numpy as np
import torch
import open_clip as oc
from PIL import Image

out_dir, emb_dir, img_dir = sys.argv[1], sys.argv[2], sys.argv[3]
model, _ = oc.create_model_from_pretrained("hf-hub:imageomics/bioclip-2")
model.eval()
scale = float(model.logit_scale.exp())
print("logit scale", scale)


class Visual(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, x):
        f = self.m.encode_image(x)
        return f / f.norm(dim=-1, keepdim=True)


vis = Visual(model).eval()
fp32 = os.path.join(out_dir, "bioclip2_visual.onnx")
torch.onnx.export(vis, torch.zeros(1, 3, 224, 224), fp32, opset_version=17,
                  input_names=["image"], output_names=["features"],
                  dynamic_axes={"image": {0: "n"}, "features": {0: "n"}}, dynamo=False)
from onnxruntime.quantization import quantize_dynamic, QuantType  # noqa: E402
int8 = os.path.join(out_dir, "bioclip2_visual_int8.onnx")
quantize_dynamic(fp32, int8, weight_type=QuantType.QInt8)

# Species subset for wildlife photography
names = json.load(open(os.path.join(emb_dir, "txt_emb_species.json"), encoding="utf-8"))
emb = np.load(os.path.join(emb_dir, "txt_emb_species.npy"), mmap_mode="r")
CLASSES = {"Aves", "Mammalia", "Squamata", "Amphibia", "Testudines", "Crocodylia", "Sphenodontia"}
BUTTERFLIES = {"Papilionidae", "Pieridae", "Nymphalidae", "Lycaenidae", "Riodinidae", "Hesperiidae"}
keep, labels, seen = [], [], set()
for i, (tax, common) in enumerate(names):
    if tax[2] in CLASSES or tax[3] == "Odonata" or tax[4] in BUTTERFLIES:
        sci = f"{tax[5]} {tax[6]}".strip()
        if not tax[5] or not tax[6] or sci in seen:
            continue
        seen.add(sci)
        keep.append(i)
        labels.append([common, sci, tax[2], tax[3], tax[4]])
sub = np.ascontiguousarray(emb[:, keep].T).astype(np.float16)     # (n, 768)
np.save(os.path.join(out_dir, "bioclip2_species_emb.npy"), sub)
json.dump({"scale": scale, "labels": labels}, open(os.path.join(out_dir, "bioclip2_species.json"), "w",
                                                    encoding="utf-8"), ensure_ascii=False)
print("species kept", len(labels), "emb", sub.shape)

# Verify ONNX (fp32 / int8) against torch on the test photos
import onnxruntime as ort  # noqa: E402
mean = np.array((0.48145466, 0.4578275, 0.40821073), np.float32)
std = np.array((0.26862954, 0.26130258, 0.27577711), np.float32)
T = sub.astype(np.float32)
sess = {k: ort.InferenceSession(p, providers=["CPUExecutionProvider"]) for k, p in
        (("fp32", fp32), ("int8", int8))}
for f in sorted(glob.glob(os.path.join(img_dir, "*.jpg"))):
    img = Image.open(f).convert("RGB").resize((224, 224), Image.BICUBIC)
    x = ((np.asarray(img, np.float32) / 255 - mean) / std).transpose(2, 0, 1)[None]
    with torch.no_grad():
        ref = vis(torch.from_numpy(x)).numpy()
    row = [os.path.basename(f)[:22].ljust(22)]
    for k, s in sess.items():
        t0 = time.perf_counter()
        feat = s.run(None, {"image": x})[0]
        dt = time.perf_counter() - t0
        logits = scale * feat[0] @ T.T
        p = np.exp(logits - logits.max()); p /= p.sum()
        top = np.argsort(-p)[:2]
        row.append(f"{k}: {labels[top[0]][0] or labels[top[0]][1]} {p[top[0]]:.0%} / "
                   f"{labels[top[1]][0] or labels[top[1]][1]} {p[top[1]]:.0%} "
                   f"({dt*1000:.0f} ms, diff {np.abs(feat - ref).max():.3f})")
    print("  ".join(row))
for p in (fp32, int8):
    print(os.path.basename(p), f"{os.path.getsize(p) / 1e6:.0f} MB")
