# Installation and running

Photo Reviewer is a Python program. On your own computer the simplest way to use it is straight from Python. The Windows installer is only needed for a PC that doesn't have Python.

## Requirements

- **Windows 10 or 11** (macOS and Linux should mostly work but are untested; drive-letter tracking for library folders is Windows-only)
- **Python 3.10 or newer** (developed on 3.14)
- **About 1 GB of disk space** for the AI models, plus a little per photo for caches (see [Data files](data-files.md))
- **Optional:** an Intel graphics chip (Iris Xe, Arc or Core Ultra). With it the AI models run about 2× faster overall, and the eye check about 10× faster (see [AI models](ai-models.md#graphics-chip-acceleration))

### Python packages

| Package | Needed for | Tested version |
|---|---|---|
| `customtkinter` | The interface | 5.2.2 |
| `Pillow` | Image loading | 11.3.0 |
| `rawpy` | RAW files, raw highlight clipping | 0.25.1 |
| `imagehash` | Burst detection | 4.3.2 |
| `send2trash` | Sending Delete frames to the Recycle Bin | — |
| `numpy` | Scoring, everything numeric | 2.3.4 |
| `onnxruntime` | All AI features (optional: without it they are just unavailable) | 1.25.1 |
| `openvino`, `onnx` | Optional: running the models on the Intel graphics chip | 2026.4.1, 1.23.2 |

## Setting up from scratch

```bat
conda create -n Photo -c conda-forge python=3.12 pillow rawpy imagehash send2trash numpy
conda activate Photo
pip install customtkinter onnxruntime
pip install openvino onnx
```

The last line (graphics-chip acceleration) is optional. conda-forge is recommended for `rawpy`, which needs the LibRaw library.

The paths on your own computer (program folder, Python environment, shortcut target) are in `local_setup.ini` in the program folder. That file isn't committed to git, so machine-specific details stay off GitHub. Below, `<program folder>` is the folder containing `photo_reviewer.py` and `<env>` is the conda environment folder (e.g. `…\miniconda3\envs\Photo`).

## The model files

The AI features need the files in the `models` folder next to `photo_reviewer.py`, about 1 GB in total. They are not in git (too large), so keep a copy. If one is missing, that feature shows as unavailable and everything else works.

| File | Size | Feature |
|---|---|---|
| `md_v1000.0.0-spruce_640.onnx` | 29 MB | Animal detection (MegaDetector) |
| `embed_mobilenet_v3_large.onnx` | 12 MB | Keeper prediction, "same animal" for sightings |
| `bioclip2_visual_int8.onnx`, `bioclip2_species_emb.npy`, `bioclip2_species.json` | 384 MB | Species suggestions (BioCLIP 2) |
| `clip_image_int8.onnx`, `clip_text.onnx`, `clip_bpe_vocab.txt.gz` | 345 MB | AI search (OpenCLIP) |
| `owlv2_eye.onnx` | 308 MB | Eye check (OWLv2) |
| `regions_countries.json` | 16 KB | Region hint (country list) |

[AI models](ai-models.md) says where each came from and how to rebuild it with the scripts in `tools/model_export/`.

## Running

```bat
conda activate Photo
cd <program folder>
python photo_reviewer.py
```

The start screen lists recent folders, and has buttons to open a folder or **import from a card**.

### A desktop shortcut without a console window

Create a shortcut (right-click the desktop → New → Shortcut) with:

- **Target:** `<env>\pythonw.exe "<program folder>\photo_reviewer.py"`
- **Start in:** `<program folder>`

The exact lines for this computer are in `local_setup.ini`.
- **Icon** (Properties → Change Icon): `photo_reviewer_icon.ico` in the same folder

`pythonw.exe` runs without a console. If something goes wrong at start-up, run it once with `python.exe` from a prompt instead to see the messages.

### Updating

Code changes take effect the next time you start the app: there's nothing to rebuild. Your settings, learning history and caches are kept (they live in `%APPDATA%\PhotoReviewer` and in each photo folder, not in the program folder).

## Building the Windows installer (optional)

Only for putting the app on a computer without Python.

1. From an Anaconda Prompt in the program folder, with the environment active, run `build_app.bat`. It checks or installs the packages, runs PyInstaller with `photo_reviewer.spec`, and copies `models\` next to the program.
2. The result is `dist\PhotoReviewer\PhotoReviewer.exe` (a folder, not a single file).
3. For a setup program, compile `photo_reviewer_installer (2).iss` with [Inno Setup](https://jrsoftware.org/isinfo.php). It packs the whole `dist\PhotoReviewer` folder, models included, so expect an installer of over 1 GB.

> **Not yet tested:** the installer hasn't been rebuilt since graphics-chip acceleration, sightings, import, raw clipping and the eye check were added. Build it and try it on a clean machine before relying on it.
