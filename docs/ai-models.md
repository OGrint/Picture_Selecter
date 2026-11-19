# AI models

All models run on your computer with ONNX Runtime, and on the Intel graphics chip where that is faster and gives the same answers. None of them tag anything: they produce suggestions, scores and search results.

| Feature | Model | File(s) | Licence |
|---|---|---|---|
| Animal detection | MegaDetector MDv1000-spruce (YOLO, 640 px) | `md_v1000.0.0-spruce_640.onnx` | MIT |
| Keeper prediction, "same animal" in sightings | torchvision MobileNetV3-Large (ImageNet), 960-d embedding | `embed_mobilenet_v3_large.onnx` | BSD-3 |
| Species suggestions | BioCLIP 2 (Imageomics) image encoder, int8 + 48,155 species text embeddings | `bioclip2_visual_int8.onnx`, `bioclip2_species_emb.npy`, `bioclip2_species.json` | MIT |
| AI search | OpenCLIP ViT-B-32, LAION-2B (`laion2b_s34b_b79k`), image int8 + text fp32 | `clip_image_int8.onnx`, `clip_text.onnx`, `clip_bpe_vocab.txt.gz` | MIT |
| Eye check | OWLv2 base/16 ensemble (Google) with the questions built in, weights stored at fp16 | `owlv2_eye.onnx` | Apache-2.0 |
| Region hint | GBIF country list matched to iNaturalist place ids | `regions_countries.json` | data: GBIF / iNaturalist terms |

Not AI, but in the same analysis: **sharpness** (edge strength where the camera focused), **exposure**, **raw highlight clipping** (read straight from the sensor data) and **eye sharpness** (a blur measure). They're described below.

## What each one does

### Animal detection (MegaDetector)

- **What it finds:** animals, people and vehicles in a 640 px copy of every frame.
- **Problem:** it was trained on camera-trap photos, so a frame-filling bird is sometimes called a "person".
- **Correction:** each such box is checked with the species model and kept as a person only if it really looks human. An animal it misses in reeds or branches still counts as present when the species model is confident.
- **What it drives:**
  - the suggestions *No animal*, *Focus missed*, *Cut off*, *Tiny subject*, *Person*, *Vehicle*;
  - sharpness on the animal when no focus point was recorded;
  - the crop used by the species, keeper, raw-clipping and eye features.

### Keeper prediction (MobileNetV3 + your history)

- **Inputs:** an embedding of the animal crop (pose, head angle, wings, background), how it differs from the rest of its burst, plus sharpness, exposure, animal size and position, focus on the animal and sharpness rank in the burst.
- **Model:** a regularised logistic regression trained on your own picks. In every burst where you tagged a Best, that frame is "kept" and the others "not kept".
- **Training data:** past folders (`keeper_history.npz`) plus the folder you're culling, retrained a few seconds after you stop tagging.
- **When it shows anything:** predictions appear once there are at least 20 labelled bursts.
- **Accuracy check:** it's measured honestly on 20 % of bursts held back, and compared with "just pick the sharpest".

### Species suggestions (BioCLIP 2)

- **Coverage:** birds, mammals, reptiles, amphibians, dragonflies and butterflies worldwide: 48,155 species, cut down from BioCLIP 2's Tree of Life list.
- **How it runs:** once per burst, on the animal crop of a 1,600 px copy.
- **Ranking:** the raw top 25 are re-ranked by your confirmed species (`species_history.json`) and the region hint. Species recorded in the folder's country get 3× weight; species from a covered group never recorded there get 0.15×. Unusual ones are flagged.

### AI search (OpenCLIP)

- **What's indexed:** every photo once, a 512-d embedding saved per folder and copied offline for external drives.
- **How a search works:** your text is encoded on the CPU and compared with every photo in the library folders.

### Eye check (OWLv2)

- **Model:** OWLv2 is an open-vocabulary detector, so it can be asked to find things described in words. The questions "an eye", "a bird's eye", "an animal's eye" and "a head" are built into the model file, so it takes just the picture.
- **What it looks at:** the animal crop from the full-resolution preview, resized to 960 × 960 (the D850's embedded preview is full size, so even a small bird's eye is found).
- **Eye sharpness:** the Crété-Roffet blur measure, which compares the eye with a re-blurred copy of itself. Contrast doesn't matter, only how wide the edges are.
- **When it runs:** after the main analysis, with the photo on screen first.

**Tested on the owner's own D850 frames** (92 animal crops from 9 folders, graded by eye):

| Finding | Consequence in the app |
|---|---|
| Confidence ≥ 0.35: on the eye in nearly every case, including dark eyes on black heads (avocets, turnstone, grackle), lions, cheetah, rhinos | Eye box used for sharpness, E and P |
| Confidence < 0.3: almost always no visible eye (head turned, tucked or under water, yawning, distant bird). A few real misses, e.g. a white egret's eye next to its yellow bill | *Eye: not found*. Never a reason to tag anything |
| Eye sharpness **across different photos** separates sharp from soft only ~80 % of the time (noise and feather texture fool it) | The app never says "sharp" or "soft" on its own |
| **Within a burst** (same animal, light, ISO) it ranks the frames the way they look: equally sharp eyes land within ~0.02, visibly softer ones ≥ 0.045 below the best | *Sharpest in this burst*, *as sharp as the best* (within 0.035), *softer than DSC_…* (≥ 0.035 below) |
| 8-bit version: a different spot on 44 of 92 frames | Not used |
| Weights stored at fp16: identical on 92 of 92, half the size | Used (308 MB) |

### Raw highlight clipping (no model)

- **Why the camera's level can't be used:** the white level the camera reports isn't where it actually saturates. On the D850 the reported level is 15311. Real clipping happens at 16188–16276 for green, varying by frame, and at 16380 for red and blue.
- **How clipping is found:** in each colour channel, clipped photosites all hold the channel's maximum value. That shows as a spike far above the values just below it, which an unclipped channel never has. The app reads every second Bayer quad.
- **Animal versus background:** clipping that runs on outside the animal's box counts as background, so a blown sky behind a perched bird isn't blamed on the bird. A white bird against an equally blown sky merges with it and isn't flagged; the frame figure still shows it.
- **When it's read:** in its own pass after the main analysis, two at a time, with the photo on screen first. It takes about 1 s per NEF.

## Graphics-chip acceleration

With `openvino` and `onnx` installed and an Intel graphics chip present (`graphics_acceleration` on):

1. **Start-up:** every model starts on the CPU, so analysis begins immediately.
2. **Preparing a copy:** in the background, each model is compiled for the graphics chip (fp16).
   - **int8 models** (species, search) can't run there as they are: their integer arithmetic overflows in fp16. They're first converted to fp16 weights in a separate helper process, once, and saved in `%APPDATA%\PhotoReviewer\gpu_models` (about 800 MB).
3. **Switching over:** a model is switched to the graphics chip only if its output agrees with the CPU (cosine ≥ 0.95) and it's at least 1.15× faster. Otherwise it stays on the CPU, so results are the same either way.
4. **Fallback:** if the graphics chip fails later (driver reset), that model goes back to the CPU for the rest of the session.

Measured on the owner's Intel Core Ultra laptop:

| | CPU | Graphics chip |
|---|---|---|
| 200 D850 NEFs, full analysis | 64 s | 30 s |
| Eye check, per frame (including loading the photo) | 5–6 s | about 1.5 s once prepared (about a minute's preparation the first time) |
| Animal detection, per frame | 84 ms | 9 ms |

The header shows **preparing graphics chip…** and then **using graphics chip**. Hover over it for the per-model detail.

## Region hint data

`regions_countries.json` maps each country to its iNaturalist place id. When you use the region hint for a country, the app downloads that country's species list once from iNaturalist (`api.inaturalist.org/v1/observations/species_counts`), saves it in `%APPDATA%\PhotoReviewer\regions\` and refreshes it after a year. That's the only network access the app makes. The request contains only the place id and taxon group, nothing about you or your photos.

## Rebuilding the models

The scripts are in `tools/model_export/`. They need PyTorch and the original model packages, which the app itself does not need. Use a separate environment:

```bat
conda create -n export python=3.12
conda activate export
pip install torch torchvision open_clip_torch megadetector onnx onnxruntime pillow numpy
```

| Model | Command | Source |
|---|---|---|
| MegaDetector | `python md_export.py md_v1000.0.0-spruce_640.onnx test.jpg` | `megadetector` package, model `MDV1000-SPRUCE` ([releases](https://github.com/agentmorris/MegaDetector/releases)) |
| MobileNetV3 | `python emb_export.py embed_mobilenet_v3_large.onnx` | torchvision `IMAGENET1K_V2` weights |
| BioCLIP 2 | `python bioclip_export.py <out_dir> <emb_dir> <test_images_dir>` | `hf-hub:imageomics/bioclip-2`; `<emb_dir>` = the `embeddings/` folder of the Hugging Face dataset `imageomics/TreeOfLife-200M` (`txt_emb_species.npy/.json`) |
| OpenCLIP | `python clip_export.py <out_dir> <test_images_dir>` | `ViT-B-32`, `laion2b_s34b_b79k`; also copies the tokenizer vocabulary |
| Eye model | `python bake_eye.py model.onnx owlv2_eye_full.onnx`, then `python halve_weights.py owlv2_eye_full.onnx owlv2_eye.onnx` | `onnx/model.onnx` from Hugging Face `onnx-community/owlv2-base-patch16-ensemble-ONNX` (run in the app's environment: it uses the app's tokenizer) |
| Region list | `python build_regions.py gbif_countries.json regions_countries.json` | GBIF country enumeration (`api.gbif.org/v1/enumeration/country`), looked up in the iNaturalist places API |

Each export script checks the ONNX output against the original model and prints the difference. Copy the results into `models\`.

**If you change a model,** change its id constant too (`MODEL_ID` in `reviewer_detect.py`, `reviewer_species.py` and `reviewer_eyes.py`; `INDEX_VERSION` in `reviewer_search.py`). The per-folder caches made with the old model are then ignored and redone, instead of mixing old and new results.
