# Photo Reviewer — User Guide

**Version 1.0**

Reference pages: [installation](docs/installation.md) · [quick start](docs/quick-start.md) · [keyboard shortcuts](docs/keyboard-shortcuts.md) · [settings](docs/settings.md) · [data files](docs/data-files.md) · [AI models](docs/ai-models.md) · [troubleshooting](docs/troubleshooting.md)

---

## Contents

1. [What is Photo Reviewer?](#1-what-is-photo-reviewer)
2. [Getting Started](#2-getting-started)
3. [The Main Interface](#3-the-main-interface)
4. [Tagging Images](#4-tagging-images)
5. [Navigating Your Session](#5-navigating-your-session)
6. [Star Ratings](#6-star-ratings)
7. [Gallery View](#7-gallery-view)
8. [Burst Detection and Triage](#8-burst-detection-and-triage)
9. [Quality Scoring](#9-quality-scoring)
10. [Compare Mode](#10-compare-mode)
11. [Display Tools](#11-display-tools)
12. [Multi-Pass Culling](#12-multi-pass-culling)
13. [Metadata and Keywords](#13-metadata-and-keywords)
14. [Folder Tree](#14-folder-tree)
15. [Send to Topaz](#15-send-to-topaz)
16. [XMP Sidecars](#16-xmp-sidecars)
17. [Backup Warning](#17-backup-warning)
18. [Keyboard Shortcuts](#18-keyboard-shortcuts)
19. [Supported File Formats](#19-supported-file-formats)
20. [Session Data and Caching](#20-session-data-and-caching)
21. [Tips for Wildlife Photographers](#21-tips-for-wildlife-photographers)

---

## 1. What is Photo Reviewer?

Photo Reviewer is a fast photo culling and review tool designed for wildlife and nature photographers. It is built for the step between downloading your images and editing them — helping you quickly identify your best shots, delete obvious rejects, group burst sequences, score image quality, and route keepers to your editing software.

It works with RAW files from all major camera manufacturers and does not modify your original images in any way.

---

## 2. Getting Started

### Opening a session

When you launch Photo Reviewer you will see the start screen. Click **Open Image Folder** and select the folder containing your images. The app will scan the folder and open your session.

If you have opened folders previously they will appear as **Recent Folders** on the start screen — click any of them to reopen instantly.

### Importing from a memory card

Click **📥 Import from card…** on the start screen. The card folder is filled in automatically when a card with a `DCIM` folder is inserted.

- **Destination: Everyday / Holiday** — two saved setups, each remembering its own *Copy to* folder, backup folder, year folder and grouping. For example Everyday → your laptop's photo folder, per day; Holiday → the external drive, per week. Your previous import destination becomes the Everyday setup.
- **Put folders inside a year folder** — adds a `2026` folder above the shoot folders (e.g. `Photos\2026\2026-06-02 Minsmere`).
- **Backup copy to (optional)** — a second copy made at the same time, e.g. on your external drive, so the photos are in two places before you format the card. Leave it empty (✕) to make just one copy.
- **Group into** — *Per day* (`2026-06-02 Masai Mara`), *Per week* Monday–Sunday, named by the days actually covered (`2026-06-01 to 06-07 Kenya`), *Per month* (`2026-06 Garden birds`) or *One folder* for the whole import (`2026-06-01 to 06-14 Kenya`). The window shows an example of where folders will go.
- **Verify every copy** — each copy is read back and checked (slower, safest).
- **Open the first folder for culling while the rest copies** — start culling as soon as the first folder is done.

Copying starts straight away. The import window lists each folder with its first photo and a **label** box: type a label (e.g. *Masai Mara*) while it copies and it is added to the folder name when that folder finishes. A folder that has finished can be renamed later from the folder list (**✎ Rename**). If you import more photos from a day you have imported before, the earlier label is filled in and the new photos join that folder.

Nothing on the card is changed. Files already imported are skipped — they are recognised by name, size and capture time, so it is safe to resume an interrupted import or to re-run it with a different grouping without getting copies twice.

### What happens on open

- The first image is displayed immediately — you can start culling straight away
- The header shows what is happening in the background and roughly how long is left:
  - **Preparing folder** — reading capture times and focus points and building the burst groups
  - **Analysing** — scoring every image and looking for animals
- All previous tags and ratings from your last session are restored automatically

On a laptop with Intel graphics the AI models (animal detection, species, keeper prediction, AI search) run on the graphics chip, which makes analysis about twice as fast (roughly 16 → 7 minutes for 3,000 D850 frames on an Intel Core Ultra laptop). While it gets ready the header says **preparing graphics chip…** and then **using graphics chip**. Analysis starts on the CPU straight away and switches over once each model is ready. Each model is only switched after a check that the graphics chip gives the same answer as the CPU and is faster; otherwise it stays on the CPU, so the results are the same either way. The first time, the app makes a graphics-chip copy of the species and search models (a few seconds, in the background, about 400 MB in `%APPDATA%\PhotoReviewer\gpu_models`). It works on mains or battery. To turn it off, set `"graphics_acceleration": false` in the settings file.

Progress is saved as it goes, so if you close the app part-way through it carries on from where it stopped next time. Re-opening a folder that has been fully analysed does no analysis again. If the folder can't be written to (a locked card or read-only folder) you are warned, because nothing could be saved.

### Resuming a session

Your tags, ratings, scores, and hashes are saved to JSON files in your image folder as you go. Close the app at any time and reopen the folder: everything is restored. The folder opens at its first photo. Press **R** (or **⇥ Resume**) to jump to the furthest photo you've tagged or rated, then carry on with →. **Tab** instead finds the next photo you haven't tagged yet.

---

## 3. The Main Interface

### Header row (top bar)

| Element | Description |
|---|---|
| **📷 Photo / Reviewer** | App identity — click Viewer to return to single image view from any overlay |
| **Viewer** | Returns to single image view. Highlighted green when active |
| **Pass** | Multi-pass culling mode toggle — see section 12 |
| **Auto→** | Auto-advance: off / after Delete / after Best and Delete |
| **Filename** | Current image filename — large and bold for easy reference |
| **Peak / Loupe / 1:1** | Display tools — see section 11 |
| **Rate: ☆☆☆☆☆** | Star rating for current image — click to rate |
| **Progress** | Current position e.g. 11 / 14 (78%) |
| **⛶** | Toggle fullscreen |
| **✕** | Quit (asks for confirmation) |

### Workflow bar (second row)

Contains the main view buttons: Gallery, Compare, Clean Load, Bursts, and ? (help).

### Rename bar

Shows the current filename with an editable field. Click the name to edit, press Enter to confirm, Escape to cancel. The file type badge (JPEG, NEF etc.) is shown to the right. The image's XMP sidecar is renamed along with it, so ratings and keywords stay attached.

### Camera bar

A thin bar showing shooting parameters for the current image: camera model, shutter speed, aperture, ISO, focal length, and date. Read from EXIF automatically.

### Canvas (main image area)

The current image displayed at fit-to-window or your chosen zoom level.

### Filmstrip

A strip of thumbnails along the bottom showing up to 20 images either side of the current one. The current image has a white border. Tag colours appear as stripes: green = Best, red = Delete, blue = ID, orange = Compare. A thin green progress bar above the filmstrip shows your position in the session.

### Sidebar

The right-hand panel with accordion sections: Search (with **Browse images…** for a filterable filename list), Session counts, Quality scores, Histogram, Metadata, Keywords, EXIF.

### Footer

Action buttons: ← Prev, Next →, ★ Best, ✕ Delete, ⚑ ID, ⊞ Compare, → Topaz, ▶ Process. Keyboard shortcuts are shown beneath each button.

---

## 4. Tagging Images

Images can be tagged in three ways. Best and Delete exclude each other — tagging an image Delete removes its Best tag and vice versa — but ID can be combined with either.

| Tag | Button | Keys | Colour |
|---|---|---|---|
| **Best** | ★ Best | B or Enter | Green |
| **Delete** | ✕ Delete | D or Backspace | Red |
| **ID** | ⚑ ID | I, Space, or + | Blue |
| **Compare** | ⊞ Compare | X | Orange |

Pressing a tag key a second time removes that tag (toggle).

### Auto-advance on Delete

The **Auto→** button in the header cycles through three settings: **OFF**, **Delete** (pressing D moves on to the next image) and **Best+Del** (B and D both move on). This is the fastest way to cull a large session. The setting is remembered.

### Undo

Press **Ctrl+Z** to undo the last change — a tag, star rating, keyword, or metadata edit. Up to 50 undo steps are available per session.

### Processing your tags

Click **▶ Process** in the footer when you have finished culling. After you confirm:

- **Best** images are copied into a `Best/` subfolder (with their XMP sidecars)
- **ID** images are converted to full-quality JPEGs in an `ID/` subfolder
- **Delete** images are moved to the Recycle Bin (with their sidecars)

A progress panel shows how far it has got and roughly how long is left. While Process runs, the background passes (raw clipping, eye check, search indexing) pause, so the copies get the drive to themselves. Copying Best files within the same USB hard drive is still limited by the drive's speed. Converting an ID RAW to JPEG takes several seconds each.

**Naming the Best copies.** In the Process dialog, tick **Rename the copies in Best/** to name them for the rest of your workflow — by default `Species_Date_Location_001`, e.g. `Grey-Heron_2026-05-12_Minsmere_001.NEF`. Topaz and darktable then carry those names through, so no renaming is needed after export. The originals keep their camera names.

- **Review every name before processing:** with renaming ticked, the dialog lists every Best photo with the name its copy will get. Click a row (or select rows and press Space) to untick it; an unticked photo's copy keeps its original camera name. **Tick all**, **Untick all** and **Only confirmed species** set them all at once. Names that come from an unconfirmed species suggestion are shown in amber, and *Unidentified* ones in red. The numbering closes up over the ticked photos only.
- Fields you can use: `{species}` `{date}` `{location}` `{n}` (001, 002… per species, date and location, in capture order) `{time}` `{orig}` (original name)
- **Species**, **Date** and **Location** come from the NAME FOR BEST panel: what you confirmed, otherwise the species suggestion, the capture date and the folder label. The dialog tells you how many names come from unconfirmed suggestions; images with no species at all are named *Unidentified*
- Set species or location for many images at once: select them in the gallery or burst view and use **Species…** / **Location…** on the selection bar
- A RAW + JPEG pair gets the same name. Names are remembered, so running Process again never creates duplicates Images that an older version left tagged both Best and Delete are listed in the confirmation and skipped, so nothing is copied and then deleted. When processing finishes, a summary is shown and the start screen reappears.

---

## 5. Navigating Your Session

| Action | Keys / Controls |
|---|---|
| Previous image | ← arrow, Numpad 4, or ← Prev button |
| Next image | → arrow, Numpad 6, or Next → button |
| Jump to next unreviewed | **Tab** |
| Click filmstrip thumbnail | Jump directly to that image |
| Click gallery cell | Jump to image and close gallery |

### Jump to next unreviewed

Press **Tab** to jump to the next image that has no tag (Best, Delete, ID) and no star rating. Useful for resuming a half-finished session. Wraps around to the beginning if needed. Shows a message when all images have been reviewed.

### Pre-loading

The app pre-loads the ±3 images around the current position in the background. Navigation between adjacent images is therefore near-instant even for large RAW files.

---

## 6. Star Ratings

Click the **Rate: ☆☆☆☆☆** stars in the header or press keys **1–5** to set a rating. Press the same number again or **0** to clear the rating.

| Stars | Meaning (suggested) |
|---|---|
| ★☆☆☆☆ | Marginal — keep but low priority |
| ★★☆☆☆ | Decent — worth processing |
| ★★★☆☆ | Good — process this session |
| ★★★★☆ | Excellent — prioritise |
| ★★★★★ | Portfolio / exceptional |

Ratings are stored in the session JSON and written to XMP sidecar files, making them readable in Lightroom and Capture One without re-rating.

---

## 7. Gallery View

Press **G** or click **Gallery** in the workflow bar to open a grid view of all images in the session.

### Filtering

Use the filter pills at the top right to show only: All, ★ Best, ✕ Delete, ⚑ ID, or Untagged images (no tag and no star rating). Type in the filter box to search by filename.

### Sequence separators

Images shot more than 5 minutes apart are separated by a green divider bar showing the time of the new sequence. This immediately reveals the structure of a shoot — each location or subject group appears as a distinct cluster.

### Tagging from gallery

Each cell has ★ / ✕ / ⚑ buttons so you can tag images without opening them. **Double-click** a thumbnail to open it in the main viewer.

### Keyboard and selection

The gallery, Clean Load and burst views can be driven from the keyboard:

- **Arrow keys** (and Page Up / Page Down / Home / End) move a yellow cursor; **Enter** opens that image
- **Click** selects an image, **Ctrl+click** adds or removes one, **Shift+click** or **Shift+arrows** selects a range, **Ctrl+A** selects everything shown
- **B / D / I** tag, and **1–5 / 0** rate, the selection (or the image under the cursor)
- A bar above the grid offers the same actions for the mouse, plus adding a keyword, species or location to every selected image
- **Ctrl+Z** undoes a whole batch in one step; **Escape** clears the selection, and a second Escape closes the view

The **⚠ Check** filter shows only images with a suggestion (see below).

### Dynamic columns

The grid automatically adjusts column count to fill the available width as you resize the window.

Press **G** again or click **← Back** to return to single image view.

---

## 8. Burst Detection and Triage

### What is burst detection?

Photo Reviewer automatically identifies groups of images taken in rapid succession of the same subject. It uses visual similarity hashing (comparing image content) combined with a timestamp gate — images more than 30 seconds apart cannot be in the same burst even if they look similar.

This prevents false groupings of visually similar scenes shot at different times (e.g. two different tree canopy shots).

### Running burst analysis

Click **Bursts** in the workflow bar. If analysis has not yet run you will see a loading screen while it processes. Results appear in the burst triage view. While you are inside a burst, the header shows your position in it (e.g. *Burst 3/8*).

### Sightings view — review a whole encounter at once

Click **◎ Sightings** (or press **V**). Bursts are joined into **sightings**: the same animal, photographed without a long break. A new sighting starts when the species changes (a confirmed species, or a confident suggestion) or after a gap with no photos — 3 minutes by default, adjustable at the top (1–60 min). When the species isn't known, bursts are joined if the animal looks the same.

- The left list shows each sighting: time, species (✓ once confirmed), number of frames and Best count; ✔ marks the ones you have finished
- The right side shows **every** frame of the sighting, burst by burst — blurred, motion and unusual frames are all there; nothing is hidden or pre-filtered
- **Nothing is tagged for you**: tag with **B / D / I** and stars as anywhere else (Shift / Ctrl to select several). Frames you leave untagged stay untagged
- **Species for this sighting**: the suggestion is filled in — press **N** (or pick / type another and press Enter) to set it on every frame of the sighting, so all its Best frames are named at once. One Ctrl+Z undoes it
- **Tab** — done with this sighting, go to the next unfinished one. **[ / ]** — previous / next without marking
- **Show app's pick ◆** — turn the predicted-pick marker off if you'd rather not be steered
- Species suggestions and animal analysis keep arriving in the background; press **R** to regroup with the latest
- ID Review groups ID-tagged photos by sighting too, so one decision covers the whole encounter

### Burst triage view

The left panel lists all detected burst groups by number of images. Click any group to review it.

Each image in the group shows:

- Thumbnail
- Quality scores (S = Sharpness, E = Exposure)
- **★ Best / ✕ Delete / ⚑ ID** buttons — tag individually
- **Select for batch Best** checkbox — tick multiple images then click **Mark Best & Next** to apply Best to all ticked images at once

Use **Skip →** to move to the next group without making any changes.

### What counts as a burst?

Images are grouped as a burst if:
1. Their visual content is at least 91% similar (average hash distance ≤ 6)
2. They were shot within 30 seconds of each other (capture time is read from EXIF, including for CR3, RAF and other RAW formats)

Images with no capture time in their EXIF are compared with the images next to them in filename order.

---

## 9. Quality Scoring

Photo Reviewer calculates two quality metrics for each image:

| Score | What it measures | Range |
|---|---|---|
| **Sharpness** | Edge sharpness where it matters — see below | 0–100 |
| **Exposure** | Penalty for clipped highlights or blocked shadows | 0–100 |

A **composite score** combines both. Scores are shown in the sidebar and on gallery/burst thumbnails. Every image in the folder is scored in the background after it opens.

### Sharpness at the focus point

For Nikon cameras with 153-point AF (D850, D5, D500) the app reads where the camera focused from the RAW file and measures sharpness **there** — so a sharp eye counts, and a sharp branch in front of a soft bird doesn't. With live-view AF the exact focus rectangle is used. When no focus point was recorded (manual focus, other cameras) sharpness is measured on the detected animal, and failing that on the centre of the frame. The sidebar says which was used, and the camera bar shows the AF point (e.g. *AF E9*).

Press **P** (or the **AF+Animal** button) to draw the focus point (yellow) and detected animals (cyan) on the image. The position of viewfinder AF points is calculated from the AF grid; if the yellow box is consistently offset from where you know you focused, the grid size can be adjusted with `af_grid_width` / `af_grid_height` in the settings file.

### Blown highlights from the raw data

The JPEG preview inside a RAW file shows highlights as pure white long before the sensor actually clipped, so white plumage often *looks* blown when darktable can still recover it — and it can miss one channel clipping (the red of a robin's breast). For RAW files Photo Reviewer reads the sensor data itself and works out, per colour channel, where each frame really clipped (on a D850 green clips at about 16,190–16,280 and red and blue at 16,380 — not at the level the camera reports). The sidebar shows one line, for example:

- *Raw: 2.1% of the animal blown (green)* — clipped on the bird itself
- *Raw: no highlights blown on the animal · 1.6 stops headroom* — you could have exposed brighter

Clipping that runs on outside the animal's box (a blown sky behind a perched bird) counts as background, not as the animal. A white bird against an equally blown sky merges with it and isn't flagged; the frame figure still shows it. When at least 1 % of the animal is blown you get the **Highlights blown on the animal** suggestion. For RAW files the **Exposure** score uses the raw data too, with clipping on the animal counting more than clipping elsewhere. Press **H** (or **Clip**) to see exactly where it clipped.

Unpacking a NEF's sensor data takes about a second, so this is read in its own background pass after the main analysis (two at a time), and the photo on screen is always read first. **On an external (USB) drive** only the photo on screen and Best-tagged photos are read: reading every whole NEF would keep a slow drive busy for most of an hour on a large folder and slow everything else down. Set `"raw_clip_external": "all"` to read every frame anyway. Folders you analysed before this was added get just this pass, once, the next time you open them.

### Animal detection and suggestions

If the MegaDetector model is installed (a file in the `models` folder next to the app), every image is checked for animals, people and vehicles in the background, on your own computer. MegaDetector was trained on camera-trap photos, so its results are checked by the species model: a frame-filling bird it calls a "person" is treated as an animal unless the crop really looks human, and an animal it misses in busy cover (reeds, branches) still counts as present when the species model is confident. The results only ever produce **suggestions** in the **SUGGESTIONS** panel, as a ⚠ on the filmstrip and as notes on gallery thumbnails — nothing is tagged or deleted for you:

| Suggestion | Meaning |
|---|---|
| No animal detected | The animal left the frame or was hidden — consider Delete |
| Focus point is not on the animal | The camera focused on something else — consider Delete |
| Animal cut off by the frame edge | Wing tip, tail or head clipped |
| Animal very small in the frame | Subject under 1 % of the frame |
| Person / Vehicle in the frame | Someone or something to clone out, or reject |
| Highlights blown on the animal (raw data) | At least 1 % of the animal clipped in the RAW file — press H to see where |
| Eye softer than in another frame of this burst | Another frame of the same burst has a visibly sharper eye — press E to look |

Use the gallery's **⚠ Check** filter to review all suggested images together and delete the ones you agree with in one go.

### Species suggestions and the NAME FOR BEST panel

While you do your first sweep, the **NAME FOR BEST** panel in the sidebar shows what each image will be called when it is copied to `Best/`:

| Field | Filled in with | To change it |
|---|---|---|
| **Species** | The suggested species for this burst, e.g. *Grey Heron* | Pick another from the dropdown (top 5 suggestions with their %, plus species you've used recently) or type a name, then Enter |
| **Date** | The capture date | Type a different date (YYYY-MM-DD), then Enter |
| **Location** | The folder label, e.g. *Masai Mara* from `2026-06-02 Masai Mara` | Pick a recent location or type one, then Enter |

**Region** ranks species recorded in the folder's country first. It is filled in from the folder name when it names a country (e.g. `2026-06-02 Kenya`), otherwise pick the country from the list (or *Worldwide*); your choice is remembered for the folder. The first time you use a country its species list (birds, mammals, reptiles, amphibians, dragonflies, butterflies — from iNaturalist's research records) is downloaded in a few seconds and kept; only the country is sent. Suggestions never recorded in that country are pushed down and marked *⚠ not recorded in Kenya* — worth a careful look, as it's either a misidentification or a good record.

Below them is a preview of the file name (e.g. `→ Grey-Heron_2026-06-02_Masai-Mara_###`). With **Apply to the whole burst** ticked (the default), a change applies to every frame in the burst. Press **N** to accept the suggested species without touching the mouse. The panel says whether the species is *Suggested* or *✓ Confirmed*. Changes can be undone with Ctrl+Z.

Species are suggested offline by **BioCLIP 2**, which compares the animal (cropped using the detected box) with about 48,000 species worldwide — birds, mammals, reptiles, amphibians, dragonflies and butterflies. It runs once per burst in the background, the burst you are looking at first (about a second each). Species you confirm are remembered and count for more next time, which helps with look-alikes such as Grey Heron and Great Blue Heron. Suggestions can be wrong — check them before you rely on them, especially for similar species.

### ID Review — identifying birds later

Tag anything you want to identify later with **I** (ID) while culling. Then click **🔎 ID Review** in the workflow bar. Each ID-tagged burst is one *sighting*:

- the photo is shown large (↑ / ↓ steps through the frames of the sighting; **V** or double-click opens it in the viewer for zoom and loupe)
- the five most likely species are listed with their confidence — press **1–5** to confirm one, or type a species and press **Enter**
- **U** marks it *unsure* (it stays in the list), **X** marks it *not identifiable*; you can add a note (e.g. *juvenile?*)
- confirmed sightings leave the list, and the species is written to every frame and its XMP keywords

**ID inbox (all folders)…** lists every folder under your library that still has photos to identify — double-click one to open it straight into ID Review. When you run Process, ID JPEGs of confirmed sightings are named with the species, e.g. `Grey-Heron_DSC_1234.jpg`.

### Eye check — which frame has the sharpest eye

After the analysis, Photo Reviewer looks for the animal's eye in every frame (the photo on screen first, then outwards from it) and measures how sharp it is. It uses an eye-finding model (OWLv2, in `models\owlv2_eye.onnx`) on the animal cut from the full-resolution preview, so even a small bird's eye is found — and it runs on the graphics chip when there is one (about 2 seconds a frame on an Intel laptop, 5–6 on the CPU alone; the photo you are looking at is always done first).

The sidebar then says one of:

- *Eye: sharpest in this burst* — and the frame gets **◉ Sharpest eye in burst** in the gallery and grid views
- *Eye: as sharp as the best in this burst* — the difference is too small to call
- *Eye: softer than in DSC_4268.NEF* — with the **Eye softer than in another frame of this burst** suggestion
- *Eye: not found (head turned, hidden or too small)*

Sharpness is only ever **compared within a burst** — the same animal, light and ISO. Across different photos the measure is fooled by noise and feather texture, so the app never says "sharp" or "soft" on its own; it says which frame's eye is sharper. "Not found" also covers small, distant birds whose eye is only a few pixels, and the odd bird whose eye the model misses (a white egret's eye next to its yellow bill) — nothing is ever tagged because of it.

Press **E** to jump to 100 % on the eye (press again for the whole frame), and **P** to see the eye box (green) with the focus point and animal. Results are saved in `photo_reviewer_eyes.json` in the folder, so each frame is checked once. To turn the eye check off, set `"eye_check": false` in the settings file.

### Keeper prediction

Photo Reviewer learns which frames **you** keep. Inside every burst where you picked a Best, that frame counts as "kept" and the others as "not kept". Each frame is described by an image model looking at the animal (pose, head angle, wings, composition), how it differs from the rest of its burst, and its focus-point sharpness, exposure and animal size and position. From that it predicts a **keeper chance** for every frame and marks the most likely one in each burst as the **predicted pick**:

- **◆** on the filmstrip and *◆ Predicted pick* on gallery / burst thumbnails
- the **KEEPER PREDICTION** panel shows the chance for the current image and where the burst's pick is — press **K** to jump to it
- the gallery's **Sort: keeper chance** button puts the likely keepers first

It only suggests — you still choose. It learns in three ways:

1. **As you cull** — a few seconds after you tag, it retrains with your new choices
2. **From every folder you finish** — your choices are added to its history when you close or switch folder
3. **From past folders** — click **Learn from past folders…** and choose the folder that holds your old shoots. Every folder with a Photo Reviewer session file is read in the background (progress in the header; click again to stop). Its analysis is saved in that folder so this only happens once.

It needs about 20 bursts with a Best before it makes predictions. The panel reports how often its pick matched yours on bursts it was **not** trained on, next to how often simply choosing the sharpest frame would have — so you can see how much to trust it.

### Clean Photo Load

Click **Clean Load** to score all images using the embedded JPEG thumbnail (fast — uses the preview the camera bakes into every RAW file). Scoring runs in the background with a progress bar and an estimate of the time left, so the app stays responsive; closing the view stops it and keeps the scores finished so far. Images you view are also scored automatically in the background.

### Threshold

In the scoring views, images with a composite score below the **Threshold** value get a red border; above get a green border. Adjust the threshold to match your standards.

---

## 10. Compare Mode

Use compare mode to place up to 4 images side by side.

1. Press **X** on any image to add it to the compare set — an orange stripe appears in the filmstrip to confirm
2. Add more images the same way (up to 4)
3. Press **C** or click **Compare** to open the split view
4. Each panel shows the image with filename, quality score, and ★ / ✕ / ⚑ tag buttons
5. Click any panel to jump to that image and close compare mode
6. Press **C** again or click **← Back** to close compare mode

The title bar shows how many images are in your compare set and reminds you to press C when ready.

---

## 11. Display Tools

All display tools are toggles. The button label changes to show the active state.

### Peak (F key)

Focus peaking overlay. Highlights the sharpest edges in the image in red, showing exactly where focus landed. Particularly useful for checking whether the eye of a bird or mammal is sharp. Works on the displayed image at any zoom level — more detail visible at 100 %.

### Clip (H key)

Blown highlights from the RAW data, not the JPEG preview: **red** where all three colour channels clipped (nothing left to recover), **magenta** where one or two did (detail often recoverable, but colour shifts). Follows zoom and pan. JPEG and TIFF files have no raw data, so nothing is shown for them. See *Blown highlights from the raw data* in section 9.

### Eye (E key)

Jumps to 100 % centred on the animal's eye found by the eye check (section 9); press again to see the whole frame. Small images are shown at 2×.

### Loupe (L key)

A magnified view of the area under your cursor, shown in the bottom-right corner. It uses the full-resolution preview and always shows at least 100 % (one image pixel per screen pixel) — 3× the current view when you are zoomed in further. Move your cursor to check focus on any part of the image without zooming the entire canvas. Press L or click the button again to dismiss.

### 1:1

Press **Z** (or click 1:1) to jump to 100 % — one pixel of the full-resolution preview per screen pixel — centred on the point under your mouse. Press again to return to fit. The header shows the zoom level (*Fit*, *100%*, …). When zoomed you can drag to pan. Full-resolution detail loads a moment after you stop on an image.

### Zoom

Scroll wheel zooms in and out. Drag to pan when zoomed. Press Escape to reset zoom to fit.

---

## 12. Multi-Pass Culling

Multi-pass mode lets you structure your culling into focused passes — each pass narrows down the image set.

Click the **Pass** button in the header to cycle through:

| Pass | Shows | Purpose |
|---|---|---|
| **Pass 1 · All** | Every image | Eliminate obvious failures — blurry, badly exposed, wrong subject |
| **Pass 2 · Survivors** | Images not marked Delete | Pick the best from what remains |
| **Pass 3 · Best** | Only Best-tagged images | Final review — confirm your keepers |

The button shows the current pass and the count of images in scope (e.g. **P2·Surv 11**). The arrow keys and auto-advance skip images outside the active pass, and the count updates as you tag. Changing pass keeps you on the current image when it is still in scope.

Click the button once more to exit multi-pass mode and return to showing all images.

---

### AI search — find photos by description

Click **🔍 Search** and describe what you're after — *robin in the snow*, *would make a good Christmas card*, *bird in flight against a blue sky*, *misty morning*. Results from your whole library are shown best match first.

- The first time, choose your **library folder** (e.g. the folder that holds the year's shoots). Add more with **Library folders…** — for example your year folder on the laptop *and* your holidays folder on the external drive. Every photo in them is indexed in the background (progress below the search box); new photos are added automatically, and the folder you have open is indexed as part of its normal analysis. `Best/` and `ID/` copies are skipped.
- An external drive is recognised even if Windows gives it a different drive letter. For folders on an external drive the app keeps an offline copy of the index with small thumbnails (about 10 KB a photo, in `%APPDATA%\PhotoReviewer\library`), so **results still appear while the drive is unplugged**, marked *⏏ On <drive> — plug in to open*. The Best only / Stars / Species filters work offline too.
- The **ID inbox** looks through all your library folders
- Narrow results with **Best only**, **Hide Delete-tagged**, **Stars ≥**, **Species** and **Year**
- Double-click a result to open it (switching folder if needed). Select several and use **Copy to folder…** (e.g. to gather Christmas-card candidates) or **Show in Explorer**
- Search ranks by what is in the picture, not by quality — combine it with **Best only** or **Stars ≥** to find your strongest shots

## 13. Metadata and Keywords

### Metadata fields

The **Metadata** section in the sidebar has four editable fields:

| Field | Description |
|---|---|
| **Species** | Species name |
| **Caption** | Free-text description |
| **Copyright** | Your copyright notice |
| **Location** | Location name |

Fields save automatically when you click away from them or move to another image. All data is written to the session JSON and to XMP sidecar files for Lightroom/Capture One compatibility.

### Keywords

The **Keywords** section shows your custom keyword palette. Click any keyword to toggle it on or off for the current image. Add new keywords using the text entry field at the bottom. Keywords are saved to your app settings and persist across sessions.

Keywords are written to XMP sidecar files as `dc:subject` tags, which Lightroom, Capture One and darktable read natively. The image's species is added as a keyword too.

---

## 14. Folder Tree

The left panel shows sibling folders of your current session folder — useful for switching between shoot days without reopening the app.

- Click any folder to switch to it (your current tags and edits are saved first; the new folder is analysed while a loading screen shows)
- Click **◀** in the folder panel header to collapse it and give more space to the image
- When collapsed a green **▶ Folders** button appears — click it to expand again
- **✎ Rename** (or right-click a folder) renames it, e.g. to add or change a label after import. Tags, ratings, analysis, `Best/` and `ID/` all move with it; the region choice, recent folders, learning history and search library follow the new name. If the folder is open it is saved, renamed and reopened at the same photo. Close darktable (or anything else using its photos) first if Windows says the folder is in use. A folder that is still being imported into can't be renamed until the import finishes

The panel auto-collapses if your current folder has fewer than 3 siblings.

---

### Collect Best folders into one library

**🗂 Collect Best folders…** on the start screen gathers every Best folder under a parent folder (any depth: year → month → day, holidays…) into one collection, sorted by trip and species. Originals are **copied**, never moved, so each shoot folder stays as it is.

```
Collection\
  2026\
    2026-03 Spain\
      Iberian Lynx\
        Iberian-Lynx_2026-03-14_Spain_001.NEF        original
        Iberian-Lynx_2026-03-14_Spain_001.xmp
        Iberian-Lynx_2026-03-14_Spain_001_topaz.dng  Topaz
        Iberian-Lynx_2026-03-14_Spain_001_final.jpg  darktable export
      Golden Eagle\ …
    2026-09 Scotland\ …
```

- **Which photos:** folders named *Best* or *Final best* (any capitalisation). If you re-screened a Best folder (a *Best* inside a *Best*, or *Final best* beside *Best*), only the deepest non-empty one is collected, because that's your final decision; the preview notes which were passed over. Folders such as *Best Combined* or *24 Best* aren't Best folders and are left alone (also listed).
- **Edits:** files in any sub-folder of a trip's Best folders (`tp`, `edited`, `Edited`, `tp/edited`…) are collected next to their original, never treated as duplicates:
  - **Labels:** `_topaz` for Topaz output (`tp-…`, or in a `tp` folder); `_final` for exports in an *edited* / *export* / *final* folder; `_edit` for anything else; a second of the same kind is `_final-2`.
  - **Matching:** an edit is matched to its frame by the DSC number in its name, or the capture time for renamed exports. It's matched even if the frame itself isn't in the final Best folder, as long as it's in the shoot folder; then the edit is named properly but the original isn't copied.
  - **No match:** edits with no findable original go in *Unmatched edits*.
- **Trips:** `YYYY-MM Place`, from the month most of the photos were taken in and the most descriptive folder name on the path (`26/Spain` → `2026-03 Spain`; plain day folders like `May/2` → `2026-05 May`). If a camera's clock was obviously wrong (e.g. 2000), the trip is dated from the folders instead and the files are named by month only. **Double-click a trip** in the preview to rename it.
- **Species:** what you confirmed in the app, the species already in a Best file name, or, with **Identify the species…** ticked, a suggestion from the species model on the animal MegaDetector finds. That takes about 1 s per photo the first time; results are remembered, so scanning again is quick. Suggestions are amber in the preview and *Unidentified* is red. **Select photos and use Set species…** to correct them; their edits follow.
- **Duplicates:** the same file (byte for byte) in two trips is collected once.
- **Nothing happens until you press Copy.** The preview lists every file and where it will go; click ✓ (or select rows and press Space) to leave files out. Copies are verified and can be stopped part-way. **Running it again only adds what's new**: `collection.json` in the collection folder records what came from where, earlier names never change, and new photos continue the numbering.

### Species list from your archive

**🦜 Species list from archive…** on the start screen goes through **every photo** under a folder (not just Best folders) and copies **one photo of each species per country**, with CSV lists. It's for building an ID list from an old archive. Everything is saved in a **Species list** folder inside the folder you chose (like your *Best Combined*). That folder is never read itself. It copies only; your originals are never changed.

```
<the folder you chose>\Species list\
  All species.csv               every species, country, dates, how it was identified
  Countries.csv                 per country: species, how many of each group, dates, to clarify
  Species by country.csv        one row per species, a column per country (date first seen there)
  Species by year.csv           one row per species, a column per year (sightings that year)
  To clarify.csv
  Species list.xlsx             all of the above in one Excel workbook, a tab per list
  Spain\
    Spain species.csv
    Birds\
      Eurasian Hoopoe (Upupa epops)_2019-05-12.NEF
    Mammals\
      Iberian Lynx (Lynx pardinus)_2019-03-14.NEF
    To clarify\
      Willow Warbler (Phylloscopus trochilus) - possible\
        2019-03-15_DSC_0200 (Willow Warbler 45%, Chiffchaff 30%, Wood Warbler 9%).NEF
  Kenya\ …
  Unknown country\ …
```

1. **Find folders.** Every photo is found and grouped by place, with its country:
   - **From folder names:** *Spain*, *Kenya 2019*, *UK*…
   - **From XMP sidecars:** a country entered in darktable or Lightroom.
   - **Otherwise it's red.** Folders are shown as a tree. Select one or more (Ctrl / Shift + click) and use **Set country…** (or double-click). A country set on a folder applies to **every folder under it**, except folders with a country of their own: named after one (*Spain*), from XMP, or set by you. So set *UK* on the year folder and only *Spain* keeps its own. *Automatic* removes what you set, so the folder inherits again. Your choices are remembered. The D850 doesn't record where a photo was taken, so most countries come from folder names.
2. **Identify species.** This runs in the background and can take hours for a big archive on the external drive. **Stop** whenever you like: everything read is remembered, so pressing Identify again carries on where it stopped, and photos already read take no time.
   - **Your own IDs:** species you named in the app, or that are already in an app file name, count as certain.
   - **Everything else:** the species model reads the animal MegaDetector finds and re-ranks its guesses with the country's species list, as in the app.
   - **Certain** means at least the **Certain at** probability (80 % unless you change it) **and** recorded in that country.
   - **To clarify:** anything less is filed under its best guess, with the top three guesses in the file name. **To clarify: up to … photos per best guess** (1, 3, 7, 10 or All; 7 unless you change it) limits how many are copied. For example, if 40 uncertain photos in the UK all have *Willow Warbler* as the best guess, the best 7 (from different days where possible) go into *United Kingdom\To clarify\Willow Warbler (Phylloscopus trochilus) - possible*. Guesses that are also certain in that country are included.
   - **Rarities:** a species with very few iNaturalist records in that country (fewer than 25, or half the country's typical count where records are sparse) goes to *To clarify* too, however sure the model is. Beluga in Scotland (9 records) and Franklin's Gull in Spain (16) are examples.
   - **Out of range:** a bird never recorded in that country can still be the answer (vagrants turn up), so it's only marked down a little, and goes to *To clarify* to check. A mammal, reptile or amphibian never recorded there almost never is, so it's marked down hard. If a relative in the same genus is recorded there, that relative wins (Red Deer, not Elk, in the UK). Dragonflies and butterflies sit in between, as some migrate.
   - **Organised by type, species and year:** the lists run Birds A–Z, then Mammals A–Z and so on, with the **Years** each species was seen. *Species by year.csv* (and its tab in the workbook) has a column per year with how many sightings there were that year.
   - **Bursts:** a burst (frames at most 2 s apart) counts as one sighting. One frame is read, a Best-folder frame first, and more only while it stays uncertain. A mixed flock in a single burst gives only its clearest species.
   - **Duplicates:** the same photo in a shoot folder and its Best folder is read once.
   - **No animal:** frames without one are skipped.
3. **Copy list.** The photo kept for each species is the best one found: in a Best folder, the animal large in the frame, a confident ID. Copies are verified. Running it again keeps the photo already chosen for a species, and adds only new species and new photos to clarify.
4. **Review…** goes through the list in the app, like **ID Review**: one photo at a time, large, with the model's top three guesses. Pick **Show** *To clarify* (the default), *Name check ⚠* (species whose AI name looked odd) or *All species*, and a **Country** (or *All countries*).
   - **1–3** (or click) picks a guess. Guesses already in that country's list are marked **✓ in list**.
   - **Type your own name** (common or scientific; the list offers matches, the country's own names first) and press **Enter**.
   - **Duplicates:** if the species is already in that country's list, the photo there is shown underneath. **Enter** keeps that photo and removes this copy. **Shift+Enter** (or Shift+1–3, Shift+click) keeps this photo instead.
   - **Del** (or X) removes the photo: no animal, unidentifiable, or not wanted.
   - **Removing a confident species' photo** (under *All species*) removes only that photo, not the species. If the species was seen elsewhere in that country, its next best photo takes its place straight away and is shown for you to check (remove that too if it's also wrong). If there's no other confident sighting, uncertain photos in that country that had it as one of their guesses go into *To clarify*, so a species the AI got wrong on one photo isn't lost if you really did see it.
   - **Side by side (G)** shows every photo filed under the same best guess at once (or a species' other sightings), so look-alikes and odd ones out stand out. Click one to pick it. Remembered for next time.
   - **Compare with iNaturalist (I)** puts photos of the species in the box (or the best guess) from iNaturalist next to yours. Click it for the next photo (up to 6); right-click opens the species' iNaturalist page. Only the scientific name is sent; each species' photos are downloaded once and kept. The photo's credit is shown under it.
   - **Ctrl+Z** (or **↶ Undo**) takes back the last change, as far back as you like in this session: files, lists and what the AI learns from. Removed copies wait in a hidden folder and go to the Recycle Bin when you close Review.
   - **Reason / note** (optional): why it's this species (*juvenile*, *female*…) or why it's removed (*duplicate*, *no animal*…). Pick one or type your own. It's saved with your decision and shown in the lists' **Your note** column.
   - **← →** next / previous photo · **↑ ↓** the burst's other frames · **V** (or double-click) opens the original · **Esc** closes.
   - The copies in *Species list* are moved, merged or removed to match, and the CSV lists are rewritten after every decision. Removed copies go to the Recycle Bin; your originals are never changed. Renaming a species under *All species* renames every sighting of it in that country.
   - Your decisions are remembered (in `species_list.json`): Identify again treats a photo you named as your own ID, and leaves out a photo you removed.

   **The AI learns from your decisions.** Every photo you confirm in Review, whether you accept the AI's guess or correct it, becomes an example: the photo's fingerprint (what the species model sees) and your answer. The next time you press **Identify**, a photo is compared with your examples. The more it looks like the ones you called *Chiffchaff*, the more *Chiffchaff* is favoured. It's a soft pull, not a rule: one example changes little, many similar ones a lot, and a photo that doesn't look like any of them is left as the AI sees it. How strong the pull is gets tested on your own decisions, holding out each day's photos in turn while it learns from the rest. If it doesn't make the answers better, it stays off until there are more examples. It needs at least 8 decisions from 3 days to start.
   - After Identify, the summary says how many decisions it learned from, how often it was right on held-out photos before and after, and how many results it changed. Changed results show *learned* in the list, and *To clarify* photos show how, e.g. *AI said Willow Warbler 45%; Chiffchaff 30% → 84%*. A changed result still has to pass *Certain at*.
   - Photos read before this was added are re-read once, only the uncertain ones, to get their fingerprint. That takes minutes, not hours.
   - The examples are shared by every list you make, so decisions on 2026 help with your holiday archive.
   - **Your other IDs teach it too:** species you confirm in the main window (ID Review, or the name panel) become examples, and so do species you named in the past (up to 6 photos each, from different days, found when Identify runs). The main window's suggestions also use what it has learned: the line under the region (and under ID Review's suggestions) says how many of your IDs it has learned from and how much better it got on photos held out.
   - Removed photos aren't examples (there's no answer to learn), and nor are names the AI doesn't know. The AI can't read your note; it learns from the photo and the name you give it.

   **Review straight away:** the last results for a folder are kept, with your Review changes, so when you open the window again **Review…** and **Copy list** work without identifying again. Press Find folders and Identify species to include new photos or changes.

**Groups covered:** birds, mammals, reptiles, amphibians, dragonflies, butterflies and moths. Other animals (fish, beetles, spiders…) usually end up in *To clarify* under the closest guess. Domestic animals can too (a Highland cow guessed as *Yak?*).

**Names:** each species is named *Common name (Scientific name)*. A few species are listed twice in the AI's list under slightly different scientific names (*Dendrocopos* / *Dendrocopus major*, the Great Spotted Woodpecker): those are counted as one, with their chances added together. Common names are the British English names from iNaturalist (downloaded with the country's species list), because the model's own are sometimes American or odd. Where they differ, the list's **Name check** column shows the model's name: *AI: Spanish Lynx* is a variant; *⚠ the AI called it Elk* (for Red Deer) means the two names have nothing in common, and is amber in the window. Names you gave in the app are kept as they are. Without a country, the model's name is used and marked *name not checked*.

**Changing your mind:** change a country, *Certain at* or the number per guess, then press Identify again. Re-ranking is instant, because nothing needs reading again. What you decided in **Review…** is kept.

**Speed:** on the laptop drive, all of 2026 (9,549 photos) took about 7 minutes, because only 3,753 needed reading (bursts and duplicates are read once). The external USB drive is slower; leave a big archive running overnight. Running it again takes seconds.

## 15. Send to Topaz

Click **→ Topaz** in the footer to send images to Topaz Photo AI for noise reduction or sharpening.

On first use you will be asked to locate the Topaz Photo AI executable. The path is saved for future sessions.

You can send:
- **Current image only** — click Yes in the prompt
- **All Best-tagged images** — click No in the prompt

Topaz opens with the selected files. Photo Reviewer remains open in the background. Very large selections are sent in batches, because Windows limits how many file paths can be passed at once — you are asked first, and Topaz may open once per batch.

---

## 16. XMP Sidecars

Photo Reviewer automatically writes an XMP sidecar file alongside each image whenever you apply a tag, rating, keyword, or metadata change.

For an image named `DSC_0302.NEF`, the sidecar is written as `DSC_0302.xmp` in the same folder.

XMP sidecars are read natively by:
- Adobe Lightroom Classic
- Capture One
- Topaz Photo AI
- Adobe Bridge
- Most other professional editing software

The sidecars contain:
- **Rating** (xmp:Rating) — your 1–5 star rating
- **Label** (xmp:Label) — Pick (Best) or Reject (Delete)
- **Keywords** (dc:subject) — your keyword tags
- **Caption** (dc:description)
- **Copyright** (dc:rights)

You do not need to do anything to trigger XMP writing — it happens automatically on every tag, rating, keyword or metadata change (and on undo).

**Existing sidecars are preserved.** If an image already has an XMP written by Lightroom, Capture One or another program, Photo Reviewer updates only its own fields (rating, Pick/Reject label, caption, copyright) and adds its keywords to the existing ones. Develop settings and colour labels in the file are left untouched.

**RAW + JPEG pairs.** If a folder contains both `DSC_0302.NEF` and `DSC_0302.JPG`, the sidecar `DSC_0302.xmp` belongs to the RAW file; the JPEG does not overwrite it.

**Process** copies each Best image's sidecar into `Best/` along with the image, and moves a deleted image's sidecar to the Recycle Bin with it.

---

## 17. Backup Warning

If Photo Reviewer detects that your image folder is on a removable drive (SD card, CFexpress, external USB drive) it shows an amber warning banner:

> ⚠ Source folder is on a removable drive — back up your images before marking any as Delete

This is a reminder that marking images as Delete and then removing the card without backing up first risks data loss. Click **I've backed up** to dismiss the banner for the current session.

**Always copy your images to at least one internal or NAS location before culling.**

---

## 18. Keyboard Shortcuts

### Navigation

| Key | Action |
|---|---|
| ← / → | Previous / Next image |
| Numpad 4 / 6 | Previous / Next image |
| Tab | Jump to next unreviewed image |
| R | Resume: jump to the furthest tagged image (where you got to last time) |
| K | Jump to the predicted keeper of this burst |
| N | Confirm the suggested species (for the whole burst) |
| G | Open / close gallery |
| V | Open / close the Sightings view (inside it: Tab done → next, [ ] prev / next, N species for the sighting, R regroup) |
| Z | Toggle 100 % zoom (centred on the mouse) |
| Scroll wheel | Zoom in / out |
| Escape | Close gallery / burst / scoring view |
| Drag | Pan when zoomed |
| Escape | Reset zoom to fit |

### Tagging

| Key | Action |
|---|---|
| B or Enter | Tag as Best |
| D or Backspace | Tag as Delete (auto-advances unless Auto→ is OFF) |
| I, Space, or + | Tag as ID |
| Ctrl + Z | Undo last change (tag, rating, keyword, metadata) |
| 1 – 5 | Set star rating |
| 0 | Clear star rating |

### Compare

| Key | Action |
|---|---|
| X | Add / remove from compare set |
| C | Open / close compare view |

### Display tools

| Key | Action |
|---|---|
| F | Toggle focus peaking |
| H | Show blown highlights from the raw data |
| E | 100 % on the animal's eye (again: whole frame) |
| L | Toggle loupe magnifier |
| P | Show the camera's focus point and detected animals |

### Gallery, Clean Load and burst views

| Key | Action |
|---|---|
| Arrow keys, Page Up/Down, Home/End | Move the cursor |
| Enter / double-click | Open the image in the viewer |
| Shift+arrows / Shift+click | Select a range |
| Ctrl+click / Ctrl+A | Toggle one / select all shown |
| B, D, I · 1–5, 0 | Tag / rate the selection (or the cursor image) |
| Escape | Clear selection, then close the view |

### Multi-pass

| Control | Action |
|---|---|
| Pass button | Cycle pass: All → Survivors → Best → off |
| Auto→ button | Auto-advance: off / after Delete / after Best and Delete |

### Other

| Key | Action |
|---|---|
| ? | Show / hide keyboard shortcuts overlay |
| Q | Quit (asks for confirmation) |

---

## 19. Supported File Formats

### RAW formats

| Format | Cameras |
|---|---|
| .NEF | Nikon |
| .CR2, .CR3 | Canon |
| .ARW | Sony |
| .RAF | Fujifilm |
| .RW2 | Panasonic |
| .ORF | Olympus / OM System |
| .DNG | Adobe DNG, Leica, Pentax, others |
| .PEF | Pentax |
| .SRW | Samsung |
| .NRW | Nikon Coolpix |

### Bitmap formats

| Format | Notes |
|---|---|
| .JPG / .JPEG | Full support |
| .TIF / .TIFF | Full support |

RAW file support requires rawpy to be installed (`conda install -c conda-forge rawpy`).

---

## 20. Session Data and Caching

Photo Reviewer saves several files in your image folder:

| File | Contents |
|---|---|
| `photo_reviewer_status.json` | All tags, ratings, metadata, keywords |
| `photo_reviewer_hashes.json` | Similarity hashes, capture times and focus points |
| `photo_reviewer_scores.json` | Quality scores for all images |
| `photo_reviewer_detections.json` | Animal / person / vehicle detections |
| `photo_reviewer_features.npz` | Image descriptions used by keeper prediction |
| `photo_reviewer_species.json` | Species suggestions per burst |
| `photo_reviewer_clip.npz` | AI search index for the folder |
| `photo_reviewer_eyes.json` | Eye check results (where the eye is, how sharp) |

`photo_reviewer_status.json` holds your culling decisions, so **keep it**. The others are caches: they let a folder reopen instantly, and if you delete them the app rebuilds them in the background.

**Safety:** the session file is written safely (a crash can't leave it half-written) and the previous version is kept as `photo_reviewer_status.json.bak`. If the session file is ever unreadable, it is renamed to `photo_reviewer_status.json.corrupt-<date>` rather than overwritten, and the `.bak` copy is used.

**Moved folders:** if you move or copy a folder (or a drive letter changes), your tags are still found — entries are matched by filename when the old path no longer exists.

App-wide settings (Topaz path, keyword palette, recent folders) are stored in your user profile:

| File | Location |
|---|---|
| `photo_reviewer_settings.json` | `%APPDATA%\PhotoReviewer\` |
| `photo_reviewer_recent.json` | `%APPDATA%\PhotoReviewer\` |
| `keeper_history.npz`, `keeper_model.npz` | `%APPDATA%\PhotoReviewer\` — what keeper prediction has learned (delete both to start learning afresh) |
| `species_history.json` | `%APPDATA%\PhotoReviewer\` — species you've confirmed (used to rank suggestions) |
| `regions\<country>.json` | `%APPDATA%\PhotoReviewer\` — downloaded species lists per country (refreshed yearly) |
| `import_settings.json`, `imported.json` | `%APPDATA%\PhotoReviewer\` — import setups, and the record of card files already imported |
| `library\` | `%APPDATA%\PhotoReviewer\` — offline search copies and small thumbnails for library folders on external drives |
| `gpu_models\` | `%APPDATA%\PhotoReviewer\` — graphics-chip copies of the models (about 400 MB, safe to delete) |

Every file and its format: [docs/data-files.md](docs/data-files.md).

Settings saved next to the program by earlier versions are picked up automatically the first time.

---

## 21. Tips for Wildlife Photographers

### Recommended workflow

1. **Copy from card to hard drive first** — always before opening Photo Reviewer
2. **Open the folder** — app loads and begins analysing in background
3. **First pass** (Pass 1) — navigate quickly, mark obvious deletes. Use auto-advance on Delete for speed. Focus on: is the subject sharp, is the exposure usable, is this a duplicate of a better frame?
4. **Score** — run Clean Load to score all images. Images below the threshold get a red border, so your weakest shots stand out
5. **Burst triage** — click Bursts, step through groups, pick the sharpest frame from each rapid sequence
6. **Second pass** (Pass 2) — switch to Pass 2 (Survivors). Now pick your Best shots from what remains. This is your main selection pass
7. **Compare** — use compare mode to choose between similar shots
8. **Rate** — apply star ratings to your keepers: 5 stars for portfolio, 4 for social, 3 for archive
9. **Metadata** — add species, location, copyright while images are fresh in mind
10. **Send to Topaz** — send Best-tagged high-ISO shots for noise reduction before editing
11. **Open in Lightroom** — your tags, ratings, and keywords are already written as XMP — no re-tagging needed

### Focus checking

For birds and mammals, eye sharpness is everything. In a burst, the sidebar tells you which frame has the sharpest eye — press **E** to see it at 100 %, then arrow through the burst pressing E on each. Use **Loupe (L)** to check eyes without zooming the whole canvas — move your cursor over the subject's eye and the loupe shows a 3× crop instantly.

### Exposure

Check the **Histogram** in the sidebar and the **Exposure** score: a low exposure score means a lot of clipped highlights or blocked shadows. The histogram comes from the JPEG preview and exaggerates clipping; for RAW files trust the *Raw:* line in the sidebar and press **H** — white birds that look blown on the back of the camera often aren't.

### Burst settings

The app detects bursts using visual similarity with a 30-second time gate. If you shoot the same species in different positions throughout a session and they are being grouped as bursts, this means the images are visually similar enough to trigger the threshold. This is working as intended — use the burst triage view to quickly pick the best frame from each group.

---

*Photo Reviewer — built for wildlife photographers who need to cull fast and accurately.*
