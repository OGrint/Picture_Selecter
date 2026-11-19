# Architecture

How the code is organised, how work moves between threads, and the rules changes must follow.

## Modules

```
photo_reviewer.py      UI and orchestration (run this)
├── LauncherWindow        start screen: recent folders, open folder, import from card
├── PhotoReviewer         the reviewer window: viewer, sidebar, filmstrip, folder tree,
│                         background passes, tagging, undo, Process
├── ThumbnailGrid         base for every grid view (cursor, selection, lazy thumbnails)
│   ├── InlineGalleryView     G: all photos, filters, sequence separators
│   ├── InlineScoringView     ⚡ Clean Load: lowest scorers first
│   ├── InlineBurstTriageView ⊹ Bursts
│   ├── InlineSightingsView   V: bursts grouped into sightings
│   └── InlineSearchView      🔍 AI search results (incl. offline results)
├── InlineIdReviewView    🔎 ID Review
├── HistogramCanvas, ScoreRow, CountRow, Eta   small widgets / helpers
└── make_thumb_cell       one grid cell (thumbnail, tags, scores, notes, badges)

reviewer_imaging.py    EXIF, orientation, preview / thumbnail loading, Nikon AF point,
                       sharpness + exposure score, raw highlight clipping, JPEG conversion
reviewer_storage.py    session file, caches, XMP sidecars (write + merge), settings,
                       recent folders, path remapping after a rename
reviewer_import.py     card import: reading the card, grouping, folder names, ledger,
                       verified copy, ImportDialog
reviewer_detect.py     MegaDetector + person→animal correction + suggestion codes
reviewer_predict.py    embeddings, burst grouping, keeper features, model, history
reviewer_species.py    BioCLIP 2 species suggestions, per-folder cache, confirmed history
reviewer_regions.py    country list, iNaturalist species lists, region weighting
reviewer_sightings.py  bursts → sightings (time gap, species, look)
reviewer_search.py     CLIP tokenizer + encoders, per-folder index, library roots,
                       drive tracking by volume serial, offline index
reviewer_eyes.py       eye finder (OWLv2), eye sharpness, burst comparison, cache
reviewer_accel.py      graphics-chip acceleration: Runner wraps a model, checks the
                       OpenVINO copy against the CPU, falls back; int8 → fp16 helper
tools/model_export/    one-off scripts that made the ONNX models (see ai-models.md)
tests/                 unit tests (see testing.md)
```

Only `photo_reviewer.py` and `reviewer_import.py` (its dialog) use Tk. The other modules are plain Python and are unit-tested directly.

## Opening a folder: the background passes

```
open folder ──► Preparing folder (hash, capture time, AF point; several threads)
                   │
                   ▼  bursts grouped
                Analysing (score + MegaDetector + embedding + CLIP index;
                   │       2 threads, +1 with a graphics chip; nearest photos first)
                   │       ↳ keeper retrained when it finishes
                   ▼
        ┌──────────┼───────────────┬───────────────┐
     Species     Raw clipping     Eye check      AI search index
   (1 thread,    (2 threads,      (1 thread,     (library folders,
    per burst)    RAW files)       animal frames) background)
```

- **Order and priority:** each pass starts nearest the photo you're on. The species, raw-clipping and eye passes each have a second single-thread "now" pool for the photo on screen, so its sidebar fills first. A "now" job for a photo you've already moved past is skipped.
- **Raw clipping and the eye check wait for the main analysis.** Unpacking NEFs (about 1 s each) and loading the 308 MB eye model would otherwise slow down the analysis that the keeper and species suggestions need.
- **Each pass only does what's missing.** "Missing" is judged by the cache (with file modification times), so reopening a folder is instant. A folder that needs no analysis starts the later passes straight away.

## Threads and the UI

Tk is single-threaded, so **worker threads never touch widgets**:

1. A worker computes a result and puts it on a `queue.Queue` (`_analysis_q`, `_eye_q`, `_rawclip_q`, `_species_q`, `_preload_q`, `_thumb_q`…).
2. `_poll_background` runs on the Tk thread every 60 ms and drains the queues (`_drain_background`, `_drain_eyes`, `_drain_rawclip`, `_drain_species`, `_drain_training`…), updating state and the UI.
3. Changed caches are saved a few seconds after work goes quiet (`_schedule_score_save` → `_save_scores_if_dirty`), and on close, folder switch or rename.

**`_session_gen`** is a counter bumped whenever the folder changes (open, switch, rename). Every job carries the value it started with; results with an old value are dropped. That's how a slow job from the previous folder can never write into the current one.

**Pending sets** (`_eye_pending`, `_rawclip_pending`…) stop the same photo being queued twice. Background and "now" requests have separate sets, so the photo on screen can jump the background queue.

**Undo:** every tag, rating, keyword or metadata change goes through `_set_fields`, which saves the session (debounced), writes the XMP sidecar and records one undo step. `_run_batch` groups changes to many photos into one step.

## Graphics-chip acceleration (`reviewer_accel`)

`Runner(path, threads, name, gpu=True, dequantize=False)` is a drop-in for an ONNX Runtime session (`run(None, feeds) → [output]`, single input and output).

- **Start on the CPU:** a Runner works on the CPU immediately.
- **Background compile:** a background thread compiles an OpenVINO GPU copy, times it and compares it with the CPU, and switches over only when it agrees and is faster.
- **int8 models:** with `dequantize=True` they're converted to fp16 weights by `photo_reviewer.py --make-gpu-model` in a separate process (so the conversion can't freeze the UI) and cached.
- **Clean exit:** at exit, `shutdown()` releases the OpenVINO objects, which avoids a Windows hang during DLL teardown.

A model with several inputs or outputs is first rewritten into single-input, single-output form. The eye model is an example: its text queries are built in (`tools/model_export/bake_eye.py`).

## Rules for changes

1. **Session files stay compatible.** Only *add* keys to the per-folder files; never rename or remove one, and never change what an existing key means. `save_status` keeps any entry with a set value and any unknown key. When a cached result's meaning changes, bump the model id or version constant so old caches are ignored, not misread.
2. **Never auto-tag.** Analysis produces suggestions (`_suggestions` codes, sidebar lines, badges). Only the user sets Best / Delete / ID.
3. **Never touch RAW files.** Write metadata to the sidecar (`write_xmp_sidecar`), merging into sidecars written by other programs.
4. **No Tk from worker threads.** Use a queue and a drain method.
5. **Heavy work goes in a background pass,** not in the main analysis. Put the photo on screen first and keep the UI responsive.
6. **Nothing personal leaves the computer.** The only network access is the iNaturalist species list (place id and taxon only).
7. **Keep line endings.** `build_app.bat` and `photo_reviewer.spec` must stay CRLF.

## Where to start for common changes

| Change | Look at |
|---|---|
| A new sidebar line | `_update_suggestion_panel` |
| A new ⚠ suggestion | `SUGGESTION_TEXT` / `SUGGESTION_SHORT` in `reviewer_detect.py`, and `PhotoReviewer._suggestions` |
| A new key | `_on_key` (main viewer) or a view's `handle_key`, plus the shortcut list in `_toggle_shortcuts_overlay` and [keyboard-shortcuts.md](keyboard-shortcuts.md) |
| A new background pass | Copy the eye pass: `_request_eye`, `_start_eyes`, `_eye_worker`, `_drain_eyes`, plus its executors in `_suspend_session`, folder switch and `_shutdown` |
| A new per-folder cache | A load/save pair like `load_eyes` / `save_eyes` (file names, not full paths, plus `_mtime`), loaded in `_init_session`, saved in `_save_scores_if_dirty` |
| A new setting | `self._app_settings.get("key", default)` and `save_app_settings`, plus a row in [settings.md](settings.md) |
