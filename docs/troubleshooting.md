# Troubleshooting

Run the app from a prompt (`python photo_reviewer.py`, not `pythonw`) to see its log messages. Most problems explain themselves there.

## Starting

| Problem | What to do |
|---|---|
| `ModuleNotFoundError: customtkinter` (or rawpy, imagehash…) | The wrong Python is running. `conda activate Photo` first, or use the environment's full path, `<env>\python.exe` (see `local_setup.ini`). |
| Nothing happens from the desktop shortcut | Run the same command with `python.exe` instead of `pythonw.exe` to see the error. Check the shortcut's "Start in" folder. |
| A panel says *Animal detection unavailable*, *Species unavailable*, *Eye check unavailable — model file … not found* | That model file is missing from `models\`. See [Installation](installation.md#the-model-files). |
| *…unavailable* without a missing file | `onnxruntime` isn't installed in the environment: `pip install onnxruntime`. |

## Speed

| Problem | What to do |
|---|---|
| Analysis is slow | Check the header. *preparing graphics chip…* is normal the first time (a minute or two, once). Without OpenVINO installed, everything runs on the CPU; `pip install openvino onnx` for about 2× overall. |
| The header never says *using graphics chip* | No Intel graphics chip found, or a model was slower or different there and stayed on the CPU, which is deliberate. Hover over the status for the per-model detail. `graphics_acceleration: false` or `PHOTO_REVIEWER_GPU=0` turns it off. |
| The eye check takes a long time | It's the slowest pass: about 1.5 s per frame on the graphics chip, 5–6 s on the CPU. It runs after everything else and never holds you up. The photo on screen is always done first. Turn it off with `"eye_check": false`. |
| The window freezes for a few seconds during a folder's first analysis | A known issue, mostly the first time the graphics-chip copies are being made. It clears by itself. |
| `%APPDATA%\PhotoReviewer\gpu_models` is big | About 800 MB of graphics-chip model copies. Safe to delete; they're remade once. |

## Culling

| Problem | What to do |
|---|---|
| The yellow AF box is in the wrong place | Set `af_grid_width` / `af_grid_height` ([Settings](settings.md)). It only affects the drawing and where sharpness is measured, not your files. |
| *Raw:* line missing | Only RAW files have it (JPEGs show nothing). It appears a second or two after you land on a photo. Unusual sensors (Fuji X-Trans, linear DNG) aren't supported. |
| *Raw: no highlights blown* but the preview looks white | That's the point: the camera's JPEG preview clips long before the sensor does. Press **H** to see what really clipped. darktable can recover the rest. |
| A white bird against a white sky isn't flagged *Blown* | Clipping that joins clipping outside the animal's box is treated as background. The frame figure in the *Raw:* line still shows it. Press **H**. |
| *Eye: not found* on a bird whose eye you can see | It happens on a few small or low-contrast eyes (e.g. a white egret's eye next to its bill). Use **Z** or the loupe. Nothing is tagged because of it. |
| *Eye: softer than in DSC_…* looks wrong | The comparison is only meaningful between frames of the same burst. Check both with **E**. Differences under the threshold count as "as sharp as the best". |
| Species suggestions are odd for the location | Set the folder's country in NAME FOR BEST (region). Species never recorded there are pushed down. The first use of a country downloads its list (needs internet once). |
| No keeper predictions (◆) | They appear after about 20 bursts where you've tagged a Best. **Learn from past folders…** builds that history from folders you've culled before. |

## Files and folders

| Problem | What to do |
|---|---|
| Tags missing after moving a folder or a drive-letter change | Entries are matched by file name, so they normally follow. If a whole session looks empty, check for `photo_reviewer_status.json.corrupt-<date>` or `.bak` in the folder. The app restores from `.bak` automatically when the main file is unreadable. |
| Renaming a folder outside the app | Works, but the region choice, recent list and keeper history still point at the old name. Rename with **✎ Rename** in the folder list instead. |
| Rename refused | The folder is still being imported, or the new name already exists. |
| Re-importing a card copies nothing | Those files were already imported (matched by name, size and capture time under the same destination). Delete `%APPDATA%\PhotoReviewer\imported.json` to forget all imports. |
| AI search can't find photos on the external drive | Add the folder with **Library folders…**. With the drive unplugged, results come from the offline copy (marked *plug in to open*). Photos added since it was last plugged in only appear once it has been indexed with the drive connected. |
| Lightroom or darktable doesn't show the ratings | The app writes `DSC_1234.xmp` next to `DSC_1234.NEF` (the Lightroom / Capture One convention). In Lightroom: Metadata → Read Metadata from Files. darktable keeps its own sidecar named `DSC_1234.NEF.xmp` and may not read the app's one; check with your darktable version. The file names in `Best\` (species, date, place) carry over whatever darktable does. |

## Starting fresh

- **One folder:** with the app closed, delete everything starting with `photo_reviewer_` in that folder **except** `photo_reviewer_status.json` (your tags). To lose the tags too, delete that as well.
- **The whole app:** rename `%APPDATA%\PhotoReviewer` (keep it in case). You lose settings, import setups, the keeper's learning and confirmed-species history. Your photos and per-folder sessions are untouched.
