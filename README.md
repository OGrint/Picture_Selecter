# 📷 Photo Reviewer

> A fast, feature-rich photo culling tool built for wildlife and nature photographers.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat&logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat&logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)
![Status](https://img.shields.io/badge/Status-Active-2D7A4F?style=flat)

---

## What is it?

Photo Reviewer sits between your memory card and your editing software. It is designed for the culling step — rapidly sorting hundreds or thousands of RAW files, identifying the best frames, discarding rejects, and routing keepers to Topaz, Lightroom, or Capture One.

Built specifically for wildlife photography workflows where speed matters, burst sequences are common, subject sharpness is critical, and sessions can run to thousands of images from a single outing.

Everything runs on your own computer: no cloud, no subscription, and nothing is ever tagged for you.

---

## Documentation

| | |
|---|---|
| [Installation and running](docs/installation.md) | Running from Python, model files, desktop shortcut, building the installer |
| [Quick start](docs/quick-start.md) | One shoot from card to `Best/` |
| [User guide](Photo_Reviewer_User_Guide.md) | Every feature in detail |
| [Keyboard shortcuts](docs/keyboard-shortcuts.md) | Every key, in every view |
| [Settings](docs/settings.md) · [Data files](docs/data-files.md) · [AI models](docs/ai-models.md) | Reference |
| [Troubleshooting](docs/troubleshooting.md) | Problems and fixes |
| [Architecture](docs/architecture.md) · [Testing](docs/testing.md) | Working on the code |

---

## Screenshots

| Main viewer | Gallery view | Burst triage |
|---|---|---|
| Single image with filmstrip, quality scores, histogram | Filterable grid with sequence separators and inline tag buttons | Side-by-side burst groups with per-image tag and batch select |

---

## Features

### Core culling
- **Three-tag system** — Best (B), Delete (D), ID (I) with filmstrip colour stripes (green / red / blue); Best and Delete exclude each other
- **Process** — copy Best to `Best/`, convert ID to JPEG in `ID/`, send Delete to the Recycle Bin, with a progress bar and time-left estimate
- **Name Best copies for your workflow** — e.g. `Grey-Heron_2026-05-12_Minsmere_001.NEF`, so names flow through Topaz and darktable
- **Star ratings 1–5** — stored as XMP, readable in Lightroom and Capture One
- **Auto-advance on Delete** — press D and instantly move to the next image
- **Jump to next unreviewed** — Tab key resumes any session instantly
- **Multi-pass culling mode** — Pass 1 (all) → Pass 2 (survivors) → Pass 3 (best only)
- **50-step undo** — Ctrl+Z (tags, ratings, keywords and metadata)

### Visual burst detection
- Detects burst sequences using **average hash similarity** (not just timestamps)
- **30-second time gate** prevents false grouping of visually similar scenes
- **Burst triage view** — step through groups, tag individually or batch-select keepers

### Quality scoring
- **Sharpness at the focus point** — read from Nikon 153-point AF data (D850/D5/D500) or live-view AF; falls back to the detected animal, then the frame centre
- **Exposure** — highlight and shadow clipping penalty
- **Blown highlights from the raw data** — per-channel clipping read from the sensor data, not the JPEG preview; on the animal vs the background, headroom in stops, a *Blown* suggestion, and a **Clip (H)** overlay
- **Clean Load** — background scoring via the embedded JPEG thumbnail, with an adjustable threshold and time-left estimate

### Species and naming (offline)
- **BioCLIP 2** suggests the species of every burst — ~48,000 birds, mammals, reptiles, amphibians, dragonflies and butterflies worldwide
- **NAME FOR BEST** panel during the first sweep: species dropdown (top 5 + recent), date and location, editable, applied to the whole burst; **N** accepts the suggestion
- Confirmed species are remembered and ranked higher next time
- **Region hint** — species recorded in the folder's country (auto-detected from the folder name, or chosen) rank first; unusual species are flagged. Country lists are downloaded once from iNaturalist

### ID Review and AI search
- **ID Review** — work through ID-tagged sightings with species suggestions: 1–5 to confirm, U unsure, X not identifiable, notes; **ID inbox** across all folders
- **AI search** — find photos by description across your whole library ("robin in the snow", "Christmas card"), with Best / stars / species / year filters, copy results to a folder. Several library folders (e.g. laptop + external drive); results from an unplugged drive still show from an offline copy

### Keeper prediction (learns your picks)
- Predicts which frame of each burst you'd keep — from pose/composition (image embedding of the animal), focus-point sharpness and animal size/position
- Trains on your past folders and keeps learning as you cull; reports its accuracy on held-out bursts vs "sharpest frame"
- ◆ predicted pick on filmstrip and thumbnails, **K** jumps to it, gallery sort by keeper chance — suggestions only

### Animal detection (suggestions only)
- **MegaDetector** (offline, ONNX) flags frames with no animal, focus point off the animal, animal cut off or tiny, or a person/vehicle in frame
- **Eye check** — finds the animal's eye (OWLv2, offline) and compares its sharpness across each burst: *sharpest in this burst*, *softer than DSC_…*, or *not found*; **E** jumps to 100 % on the eye
- Shown in the sidebar, filmstrip and gallery (**⚠ Check** filter) — never tags or deletes on its own

### Display tools
- **Focus peaking** (F) — red edge overlay showing sharpest regions
- **Clip** (H) — where the RAW file really clipped (red: all channels, magenta: one or two)
- **Loupe** (L) — magnified view (≥100 % of the full-resolution preview) follows the cursor for eye-sharpness checks
- **100 % zoom** (Z) centred on the mouse, with drag-to-pan

### Speed
- **Import from card**: saved Everyday / Holiday destinations, folders per day / week / month / one folder, a label per folder typed while it copies, optional verified backup copy, first folder opens for culling while the rest copies, never copies a file twice
- **Sightings view** (V): bursts of the same animal grouped into one sighting (by species and time gap); every frame shown, nothing auto-tagged, species confirmed once per sighting
- **Rename folder** from the folder list — tags, caches and settings follow
- **Whole folder analysed in the background** on open (resumes if interrupted; never repeated)
- **Graphics-chip acceleration** (Intel graphics via OpenVINO) — about 2× faster analysis; each model checked against the CPU before it is used, automatic CPU fallback
- **Keyboard and multi-select in grid views** — tag or rate many images at once, undo as one step
- **Auto-advance after Best and/or Delete**, progress and pace counter

### Gallery and compare
- **Gallery view** (G) — dynamic grid with tag filters and filename search
- **Sequence separators** — images >5 min apart split into visual groups
- **Compare mode** (C) — up to 4 images side by side with tag buttons per panel
- **Inline tagging** — tag from gallery without opening individual images

### Workflow integration
- **XMP sidecars** — written automatically on every tag/rating/keyword change; existing Lightroom/Capture One sidecars are merged into, not replaced
- **Send to Topaz** — open current image or all Best-tagged files in Topaz Photo AI
- **Folder tree** — switch between shoot days without reopening the app
- **Rename in place** — the XMP sidecar is renamed with the image
- **Backup verification** — warns when source folder is on a removable drive

### Metadata
- Species, Caption, Copyright, Location fields with XMP export
- **Custom keyword palette** — click-to-apply keywords, persisted across sessions
- Camera bar — shutter, aperture, ISO, focal length, date always visible

---

## Supported formats

**RAW:** NEF · CR2 · CR3 · ARW · RAF · RW2 · ORF · DNG · PEF · SRW · NRW

**Bitmap:** JPG · JPEG · TIF · TIFF

---

## Requirements

```
Python 3.10+
customtkinter
Pillow
rawpy
imagehash
send2trash
numpy
onnxruntime     # optional — AI features (animal detection, species, keeper, search)
openvino, onnx  # optional — run the AI models on Intel graphics (about 2× faster)
```

Install with conda (recommended for rawpy):

```bash
conda install -c conda-forge pillow rawpy imagehash send2trash numpy
pip install customtkinter onnxruntime
pip install openvino onnx     # optional: graphics-chip acceleration
```

The AI features use files in the `models` folder (about 1 GB in total):

| File | Used for |
|---|---|
| `md_v1000.0.0-spruce_640.onnx` | MegaDetector MDv1000-spruce — animal / person / vehicle detection |
| `embed_mobilenet_v3_large.onnx` | torchvision MobileNetV3-Large (ImageNet) — keeper prediction |
| `bioclip2_visual_int8.onnx`, `bioclip2_species_emb.npy`, `bioclip2_species.json` | BioCLIP 2 (Imageomics, MIT) — species suggestions |
| `clip_image_int8.onnx`, `clip_text.onnx`, `clip_bpe_vocab.txt.gz` | OpenCLIP ViT-B-32, LAION-2B (MIT) — AI search |
| `owlv2_eye.onnx` | OWLv2 base/16 ensemble (Google, Apache-2.0) with the eye / head questions built in, weights stored at half precision — eye check |
| `regions_countries.json` | Country list (GBIF) matched to iNaturalist places — region hint |

Without them the app works normally and the panels say the feature is unavailable.

---

## Running from source

```bat
conda activate Photo
cd <program folder>
python photo_reviewer.py
```

No build step: code changes take effect the next time you start it. For a desktop shortcut without a console window, and for setting up a new environment, see [Installation](docs/installation.md).

---

## Building a Windows executable

1. Install requirements above
2. Run `build_app.bat` from an Anaconda Prompt
3. Output: `dist\PhotoReviewer\PhotoReviewer.exe`
4. For an installer: compile `photo_reviewer_installer (2).iss` with [Inno Setup](https://jrsoftware.org/isinfo.php)

Only needed for a PC without Python. The installer hasn't been rebuilt since the latest features were added; test it before relying on it ([details](docs/installation.md#building-the-windows-installer-optional)).

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| ← / → | Previous / next image |
| B / Enter · D / Backspace · I / Space | Best · Delete · ID |
| 1–5 · 0 | Star rating · clear |
| N | Accept the suggested species |
| Tab | Next unreviewed |
| K | Predicted keeper of the burst |
| V · G · C | Sightings · gallery · compare |
| E · H · F · L · Z · P | Eye at 100 % · raw blown highlights · focus peaking · loupe · 100 % zoom · AF point / animal / eye boxes |
| Ctrl+Z | Undo |
| ? · Q | Shortcuts overlay · quit |

All keys, including the grid views, sightings and ID Review: [docs/keyboard-shortcuts.md](docs/keyboard-shortcuts.md).

---

## Architecture

`photo_reviewer.py` is the interface; the `reviewer_*.py` modules do the work (imaging and scoring, storage and XMP, import, detection, keeper prediction, species, regions, sightings, search, eye check, graphics-chip acceleration). Worker threads hand results to the UI through queues, and every pass works nearest the photo on screen first. Details, and the rules changes must follow (session compatibility, never auto-tag, never touch RAW files): [docs/architecture.md](docs/architecture.md).

**Key design decisions:**
- All views open **inline** (no popup windows): an overlay covers the canvas area
- **Visual similarity hashing** for burst detection (not timestamp-only)
- **XMP written on every tag**, so sidecars are always in sync with Lightroom / Capture One
- **Preload ±3 images** in a thread pool, so navigation feels instant
- **No internet required** (apart from a one-off species list per country for the region hint)

---

## Session data

The app keeps your tags in `photo_reviewer_status.json` in each photo folder (written atomically, with a `.bak`), plus caches (hashes, scores, detections, species, embeddings, search index, eye check) that are safe to delete and are rebuilt. Settings, import setups, the keeper's learning history and graphics-chip model copies live in `%APPDATA%\PhotoReviewer`. Every file, its format and whether it's safe to delete: [docs/data-files.md](docs/data-files.md). Every setting: [docs/settings.md](docs/settings.md).

---

## Recommended workflow

1. **Import from card**: Everyday or Holiday setup, folders per day / week / month, labels typed while it copies; the first folder opens while the rest copies
2. **Sightings (V)**: go through each encounter, tag Best / Delete / ID; **N** sets the species for the whole sighting, **Tab** moves on
3. **Check close calls**: the sidebar's *Eye:* and *Raw:* lines, **E** (eye at 100 %), **H** (what really clipped), **C** compare
4. **Name for Best**: species, date and location, applied to the burst
5. **ID Review** (any time later) for the frames you weren't sure of
6. **▶ Process**: Best to `Best/` (renamed), ID to JPEG, Delete to the Recycle Bin
7. **Topaz → darktable** with the Best copies; ratings and keywords are in the XMP sidecars

Step by step: [docs/quick-start.md](docs/quick-start.md).

---

## Contributing

Pull requests welcome. See [docs/architecture.md](docs/architecture.md) for how the code is organised and [docs/testing.md](docs/testing.md) for the tests (`python -m unittest discover -s tests`: 95 tests, including checks that session folders written by earlier versions still load).

---

## License

MIT — free to use, modify, and distribute.

---

*Built for wildlife photographers who need to cull fast, accurately, and without leaving the keyboard.*
