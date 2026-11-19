# Data files

Everything the app writes, where, and whether it's safe to delete.

## In each photo folder

| File | Contents | Safe to delete? |
|---|---|---|
| `photo_reviewer_status.json` | **Your work**: tags (Best / Delete / ID), stars, keywords, species, caption, copyright, location, name date, ID Review result and note, "seen" marks from the sightings view | **No**: it holds your culling decisions. (The XMP sidecars hold a copy of stars, labels and keywords.) |
| `photo_reviewer_status.json.bak` | The previous version of the session file | Yes |
| `photo_reviewer_hashes.json` | Visual hash, capture time and focus point per photo (burst detection) | Yes: recomputed on open (a few seconds per 100 photos) |
| `photo_reviewer_scores.json` | Sharpness, exposure, where sharpness was measured, raw highlight clipping | Yes: recomputed in the background |
| `photo_reviewer_detections.json` | Animals / people / vehicles found by MegaDetector | Yes: recomputed |
| `photo_reviewer_features.npz` | Image embeddings for the keeper prediction and sightings | Yes: recomputed |
| `photo_reviewer_species.json` | Species suggestions per burst | Yes: recomputed |
| `photo_reviewer_clip.npz` | AI search index for this folder | Yes: recomputed |
| `photo_reviewer_eyes.json` | Eye position, confidence and sharpness per photo | Yes: recomputed (slowest to redo: 1–6 s per photo) |
| `*.xmp` | Sidecar per photo: rating, label (Pick / Reject), keywords + species, caption, copyright | Don't: Lightroom, darktable and Capture One read them. If one already existed from another program, the app only updates these fields in it. |
| `Best\` | Copies of the Best frames (renamed if you chose to), with sidecars | Your output |
| `ID\` | JPEG copies of the ID frames | Your output |

How caches behave:
- **Check before reuse:** every cache entry records the file's modification time, so an edited or replaced photo is re-analysed.
- **Moved folders and new drive letters:** entries are matched by file name, so nothing is lost.
- **Session file safety:** it's written atomically (temporary file, then replace), so a crash can't leave half a file. If it can't be read, it's renamed to `photo_reviewer_status.json.corrupt-<date>` and kept (never overwritten), and the session is restored from the `.bak` copy.

### Session file format

`photo_reviewer_status.json` maps each photo's path to its entry:

```json
{
  "C:\\Photos\\2026\\2026-06-02 Minsmere\\DSC_1234.NEF": {
    "best": true, "delete": false, "id": false,
    "star": 4,
    "keywords": ["heron", "fishing"],
    "species": "Grey Heron", "species_sci": "Ardea cinerea",
    "location": "Minsmere", "caption": "", "copyright": "",
    "id_status": "confirmed", "id_note": "",
    "seen": true
  }
}
```

**Compatibility rule:** new versions only *add* keys. An entry is saved while any of its values is set, and unknown keys are kept as they are. Older sessions open in newer versions, and newer files open in older versions, which just ignore the new keys.

### Score entry (`photo_reviewer_scores.json`)

```json
{"sharpness": 82, "exposure": 91, "exposure_jpeg": 99, "dark": 0.0004,
 "sharp_at": "af", "v": 2, "_mtime": 1780000000.0,
 "raw": {"f": 0.00036, "s": 0.0207, "ch": "G", "hr": 0.0}}
```

| Key | Meaning |
|---|---|
| `sharpness` | 0–100, measured where `sharp_at` says: `af` focus point, `animal` detected animal, `centre` frame centre |
| `exposure` | 0–100; for RAW files highlights come from the raw data |
| `exposure_jpeg` | The exposure score from the embedded JPEG (what the keeper model learned from) |
| `dark` | Share of the frame with blocked shadows |
| `raw.f` / `raw.s` | Share of the frame / the animal with a channel clipped in the RAW data (`s` is `null` without an animal) |
| `raw.ch` | Which channels clipped: `R`, `G`, `B` |
| `raw.hr` | Headroom in stops before the brightest 0.1 % would clip (0 when something clipped) |
| `raw: {}` | The RAW data couldn't be read (not retried) |

### Eye entry (`photo_reviewer_eyes.json`)

```json
{"model": "owlv2-b16-eye-1",
 "frames": {"DSC_4268.NEF": {"p": 0.57, "s": 0.4739, "b": [0.4539, 0.5141, 0.4576, 0.5193], "_mtime": 1780000000.0}}}
```

`p` is the eye finder's confidence (below 0.3 = no visible eye), `s` the eye sharpness (only comparable within a burst), and `b` the eye box as a share of the frame. `{}` means the animal was too small to check. If the model changes (`model`), the file is ignored and redone.

## Per-user files

In `%APPDATA%\PhotoReviewer` (usually `C:\Users\<you>\AppData\Roaming\PhotoReviewer`):

| File / folder | Contents | Safe to delete? |
|---|---|---|
| `photo_reviewer_settings.json` | App settings ([Settings](settings.md)) | Resets your settings |
| `import_settings.json` | Import setups | Resets import destinations |
| `imported.json` | Record of card files already imported (`name|size|capture time` → copies) | The next import of an old card copies everything again |
| `photo_reviewer_recent.json` | Recent folders on the start screen | Yes |
| `keeper_history.npz` | What you picked in past folders: the keeper's training data | **Loses what the keeper learned.** "Learn from past folders…" rebuilds it from folders with Best tags. |
| `keeper_model.npz` | The trained keeper model | Yes: retrained from the history |
| `species_history.json` | Species you've confirmed and how often (ranks them higher) | Loses that preference |
| `regions\<ISO>.json` | Species lists per country from iNaturalist (region hint) | Yes: downloaded again when needed (refreshed yearly anyway) |
| `library\` | Offline copies of the search index for library folders on external drives, plus 192 px thumbnails (about 10 KB per photo) | Yes: rebuilt when the drive is next plugged in; unplugged search stops working until then |
| `gpu_models\` | Graphics-chip copies of the int8 models (about 800 MB, made once) | Yes: remade in the background (about a minute) |

Nothing here contains your photos, apart from the small offline thumbnails in `library\`.
