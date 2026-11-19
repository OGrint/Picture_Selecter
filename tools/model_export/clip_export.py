"""Export OpenCLIP ViT-B-32 (laion2b) image + text encoders to ONNX (int8), verify."""
import glob
import os
import shutil
import sys

import numpy as np
import torch
import open_clip as oc
from PIL import Image

out_dir, img_dir = sys.argv[1], sys.argv[2]
model, _, preprocess = oc.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
model.eval()
tok = oc.get_tokenizer("ViT-B-32")
print("logit scale", float(model.logit_scale.exp()))
print(preprocess)


class Img(torch.nn.Module):
    def __init__(self, m): super().__init__(); self.m = m
    def forward(self, x):
        f = self.m.encode_image(x); return f / f.norm(dim=-1, keepdim=True)


class Txt(torch.nn.Module):
    def __init__(self, m): super().__init__(); self.m = m
    def forward(self, t):
        f = self.m.encode_text(t); return f / f.norm(dim=-1, keepdim=True)


img_m, txt_m = Img(model).eval(), Txt(model).eval()
paths = {}
for name, mod, dummy, inp in (("clip_image", img_m, torch.zeros(1, 3, 224, 224), "image"),
                              ("clip_text", txt_m, tok(["a photo"]), "text")):
    fp32 = os.path.join(out_dir, name + ".onnx")
    torch.onnx.export(mod, dummy, fp32, opset_version=17, input_names=[inp], output_names=["features"],
                      dynamic_axes={inp: {0: "n"}, "features": {0: "n"}}, dynamo=False)
    from onnxruntime.quantization import quantize_dynamic, QuantType
    q = os.path.join(out_dir, name + "_int8.onnx")
    quantize_dynamic(fp32, q, weight_type=QuantType.QInt8)
    paths[name] = (fp32, q)
    print(name, f"fp32 {os.path.getsize(fp32)/1e6:.0f} MB, int8 {os.path.getsize(q)/1e6:.0f} MB")

vocab = os.path.join(os.path.dirname(oc.__file__), "bpe_simple_vocab_16e6.txt.gz")
shutil.copy(vocab, os.path.join(out_dir, "clip_bpe_vocab.txt.gz"))

# Tokenizer reference set for the app's own tokenizer
queries = ["would make a good christmas card", "bird in flight against a blue sky",
           "kingfisher with a fish", "misty morning", "Robin in the SNOW!", "a fox's den, 2026",
           "red berries & frost", "owl at night", "close-up portrait of an eagle", "café crème"]
ref = tok(queries).numpy()
np.save(os.path.join(out_dir, "tokenizer_ref.npy"), ref)
with open(os.path.join(out_dir, "tokenizer_ref.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(queries))

# Verify ONNX vs torch, and retrieval sanity
import onnxruntime as ort  # noqa: E402
mean = np.array((0.48145466, 0.4578275, 0.40821073), np.float32)
std = np.array((0.26862954, 0.26130258, 0.27577711), np.float32)


def prep(p):
    im = Image.open(p).convert("RGB")
    w, h = im.size; s = 224 / min(w, h)
    im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = im.size; l, t = (w - 224) // 2, (h - 224) // 2
    im = im.crop((l, t, l + 224, t + 224))
    return ((np.asarray(im, np.float32) / 255 - mean) / std).transpose(2, 0, 1)


files = sorted(glob.glob(os.path.join(img_dir, "*.jpg")))
X = np.stack([prep(f) for f in files])
with torch.no_grad():
    ti = img_m(torch.from_numpy(X)).numpy()
    tt = txt_m(torch.from_numpy(ref)).numpy()
for kind in (0, 1):
    si = ort.InferenceSession(paths["clip_image"][kind], providers=["CPUExecutionProvider"])
    st = ort.InferenceSession(paths["clip_text"][kind], providers=["CPUExecutionProvider"])
    oi = si.run(None, {"image": X})[0]
    ot = st.run(None, {"text": ref.astype(np.int64)})[0]
    label = ("fp32", "int8")[kind]
    print(label, "max diff image", float(np.abs(oi - ti).max()), "text", float(np.abs(ot - tt).max()))
    for qi in (2, 3, 7, 8):
        sims = oi @ ot[qi]
        best = np.argsort(-sims)[:2]
        print(f"   '{queries[qi]}': " + ", ".join(f"{os.path.basename(files[b])} {sims[b]:.3f}" for b in best))
