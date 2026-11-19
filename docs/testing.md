# Testing

## Unit tests

```bat
conda activate Photo
cd <program folder>
python -m unittest discover -s tests
```

95 tests, a few seconds. They need no photos and no model files: tests that need a model build a tiny one or are skipped.

| File | Covers |
|---|---|
| `test_storage.py` | Session, hash and score files from earlier versions still load; save with `.bak`; corrupt file moved aside and the backup used; moved folders matched by file name; stale cache entries ignored; XMP written, escaped and merged into Lightroom sidecars; a JPEG next to its RAW doesn't take the RAW's sidecar |
| `test_import.py` | Reading a card, grouping per day / week / month / one folder, folder names, labels, the ledger (no double copies), verified copy, folder rename / merge, settings migration |
| `test_sightings.py` | Bursts → sightings by time gap, species and look; naming a sighting |
| `test_predict.py` | Keeper learns a preference beyond sharpness, agreement measure, model and history save / load, labels from tags, burst grouping |
| `test_species.py` | Species names, prediction cache, confirmed-species history; MegaDetector "person" corrected to animal for close-up birds, animals hidden in cover found |
| `test_regions.py` | Region weighting (recorded, unrecorded, same genus), groups, country from the folder label, lookup |
| `test_search.py` | Tokenizer matches OpenCLIP's, long text, index save / load / staleness, library skips the `Best\` and `ID\` copies |
| `test_library.py` | Library folder found again after a drive-letter change, offline index round trip, path remapping after a rename |
| `test_detect_focus.py` | Nikon 153-point AF names and orientation, non-Nikon files, suggestion codes (no animal, focus missed, cut off, tiny, person) |
| `test_accel.py` | Graphics-chip Runner (CPU fallback), int8 → fp16 conversion of a hand-built model |
| `test_rawclip.py` | Per-channel saturation detection, orientation, background vs animal, raw-aware exposure, sidebar text |
| `test_eyes.py` | Eye sharpness (blur lowers it, contrast doesn't matter), burst comparison, cache, eye box mapping |

Also run a lint check (`pip install pyflakes`):

```bat
python -m pyflakes photo_reviewer.py reviewer_*.py
```

One known harmless warning: an f-string without placeholders in `photo_reviewer.py`.

## Checking a change in the real app

Unit tests don't drive the window. After a change to the UI or the background passes, check it in the app on a **copy** of a real folder (the app writes cache files into the folder it opens):

1. **Copy 20–40 frames** with a mix of subjects: a burst, a white bird, a frame with no animal, a portrait-orientation frame and a JPEG. Use a temporary folder.
2. **Use a clean settings folder** so your real settings and keeper history aren't touched. Run it from a prompt with:
   ```bat
   set APPDATA=%TEMP%\pr_test_appdata
   python photo_reviewer.py
   ```
3. **Check:**
   - the analysis finishes;
   - the sidebar fills in (Raw: and Eye: lines) for the photo on screen first;
   - your keys work: B / D / I, N, E, H, P, V, G;
   - tags survive closing and reopening;
   - reopening the folder doesn't re-analyse;
   - your folder's XMP sidecars look right in darktable.
4. **Check an older session:** open a folder culled with an earlier version and confirm the tags are all still there.

## Known test quirks

- **The keeper's pick accuracy on synthetic test data varies from run to run** (about 14–20 of 20) depending on when the training snapshot is taken. It isn't a correctness problem, but don't read much into one result.
- **The first time a fresh settings folder sees the graphics chip,** it makes the fp16 model copies (about a minute) and can briefly freeze the window (1–5 s). This also happens without the new features; it's a known issue that isn't fixed yet.
