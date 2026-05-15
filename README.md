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

---

## Screenshots

| Main viewer | Gallery view | Burst triage |
|---|---|---|
| Single image with filmstrip, quality scores, histogram, location map | Filterable grid with sequence separators and inline tag buttons | Side-by-side burst groups with per-image tag and batch select |

---

## Features

### Core culling
- **Three-tag system** — Best (B), Delete (D), ID (I) with filmstrip colour stripes
- **Star ratings 1–5** — stored as XMP, readable in Lightroom and Capture One
- **Auto-advance on Delete** — press D and instantly move to the next image
- **Jump to next unreviewed** — Tab key resumes any session instantly
- **Multi-pass culling mode** — Pass 1 (all) → Pass 2 (survivors) → Pass 3 (best only)
- **50-step undo** — Ctrl+Z

### Visual burst detection
- Detects burst sequences using **average hash similarity** (not just timestamps)
- **30-second time gate** prevents false grouping of visually similar scenes
- **Burst triage view** — step through groups, tag individually or batch-select keepers

### Quality scoring
- **Sharpness** — centre-weighted Laplacian edge detection
- **Motion blur** — gradient isotropy analysis
- **Exposure** — highlight and shadow clipping penalty
- **Clean Load** — fast scoring via embedded JPEG thumbnail
- **Detail Clean** — accurate scoring via half-resolution RAW decode (rawpy)
- **Personal threshold calibration** — app learns your Best vs Delete score distribution

### Display tools
- **Focus peaking** (F) — red edge overlay showing sharpest regions
- **Exposure inspect** (H) — shadow boost or highlight clipping view
- **Loupe** (L) — 3× magnified circle follows cursor for eye-sharpness checks
- **Noise visualiser** (N) — false-colour heat map (blue=clean → red=noisy)
- **1:1 zoom** with drag-to-pan

### Gallery and compare
- **Gallery view** (G) — dynamic grid with tag filters and filename search
- **Sequence separators** — images >5 min apart split into visual groups
- **Compare mode** (C) — up to 4 images side by side with tag buttons per panel
- **Inline tagging** — tag from gallery without opening individual images

### Workflow integration
- **XMP sidecars** — written automatically on every tag/rating/keyword change
- **Send to Topaz** — open current image or all Best-tagged files in Topaz Photo AI
- **iNaturalist species ID** — batch computer vision species identification with confidence scores
- **Folder tree** — switch between shoot days without reopening the app
- **Backup verification** — warns when source folder is on a removable drive

### Metadata
- Species, Caption, Copyright, Location fields with XMP export
- **Custom keyword palette** — click-to-apply keywords, persisted across sessions
- **Batch copyright** — apply to all Best-tagged images with one click
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
```

Install with conda (recommended for rawpy):

```bash
conda install -c conda-forge pillow rawpy imagehash send2trash numpy
pip install customtkinter
```

---

## Running from source

```bash
git clone https://github.com/yourname/photo-reviewer.git
cd photo-reviewer
python photo_reviewer.py
```

Place `photo_reviewer_countries.geojson` (Natural Earth 110m countries) in the same folder for the location map feature.

---

## Building a Windows executable

1. Install requirements above
2. Run `build_app.bat` from an Anaconda Prompt
3. Output: `dist\PhotoReviewer\PhotoReviewer.exe`
4. For an installer: compile `photo_reviewer_installer.iss` with [Inno Setup](https://jrsoftware.org/isinfo.php)

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| B / Enter | Tag as Best |
| D / Backspace | Tag as Delete |
| I / Space | Tag as ID |
| X | Add to compare set |
| C | Open / close compare view |
| G | Open / close gallery |
| Tab | Jump to next unreviewed |
| F | Focus peaking |
| H | Exposure inspect |
| L | Loupe |
| N | Noise visualiser |
| 1–5 | Star rating |
| 0 | Clear rating |
| Ctrl+Z | Undo |
| ? | Keyboard shortcuts overlay |

---

## Architecture

```
photo_reviewer.py          # ~6000 lines, single-file application
├── LauncherWindow         # Start screen with recent folders
├── PhotoReviewer          # Main app (CTkToplevel)
│   ├── ThumbnailGrid      # Base class for all gallery-style views
│   │   ├── InlineGalleryView
│   │   ├── InlineScoringView
│   │   └── InlineBurstTriageView
│   ├── InlineSpeciesView
│   ├── ComparisonView
│   └── SpeciesSuggestView
├── CountryMapCanvas       # Offline country map with point-in-polygon
└── HistogramCanvas        # RGB histogram
```

**Key design decisions:**
- All views open **inline** (no popup windows) — overlay system covers the canvas area
- **Visual similarity hashing** for burst detection (not timestamp-only)
- **XMP written on every tag** — always in sync with Lightroom/Capture One
- **Preload ±3 images** in a thread pool — navigation feels instant
- **No internet required** except for iNaturalist species ID

---

## Session data

The app saves small JSON files in your image folder:

| File | Contents |
|---|---|
| `photo_reviewer_status.json` | Tags, ratings, metadata, keywords |
| `photo_reviewer_hashes.json` | Visual similarity hashes |
| `photo_reviewer_scores.json` | Quality scores |

Sessions resume instantly with all data restored. Delete these files to start fresh.

---

## Recommended workflow

1. Copy card to hard drive
2. Open folder in Photo Reviewer
3. **Pass 1** — eliminate obvious failures with auto-advance on Delete
4. **Clean Load** — score all images, review low scorers
5. **Burst triage** — pick the sharpest frame from each sequence
6. **Pass 2** — select Best from survivors
7. **Compare mode** — resolve close calls
8. **Star ratings** — grade your keepers
9. **Metadata** — add species, location, copyright
10. **→ Topaz** — send high-ISO keepers for noise reduction
11. Open in Lightroom — XMP tags already waiting

---

## Contributing

Pull requests welcome. The codebase is a single Python file — see the architecture section above for the main class hierarchy.

---

## License

MIT — free to use, modify, and distribute.

---

*Built for wildlife photographers who need to cull fast, accurately, and without leaving the keyboard.*
