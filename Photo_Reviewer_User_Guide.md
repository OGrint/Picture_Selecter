# Photo Reviewer — User Guide

**Version 1.0**

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
13. [Species Identification](#13-species-identification)
14. [Metadata and Keywords](#14-metadata-and-keywords)
15. [Folder Tree](#15-folder-tree)
16. [Send to Topaz](#16-send-to-topaz)
17. [XMP Sidecars](#17-xmp-sidecars)
18. [Backup Warning](#18-backup-warning)
19. [Keyboard Shortcuts](#19-keyboard-shortcuts)
20. [Supported File Formats](#20-supported-file-formats)
21. [Session Data and Caching](#21-session-data-and-caching)
22. [Tips for Wildlife Photographers](#22-tips-for-wildlife-photographers)

---

## 1. What is Photo Reviewer?

Photo Reviewer is a fast photo culling and review tool designed for wildlife and nature photographers. It is built for the step between downloading your images and editing them — helping you quickly identify your best shots, delete obvious rejects, group burst sequences, score image quality, and route keepers to your editing software.

It works with RAW files from all major camera manufacturers and does not modify your original images in any way.

---

## 2. Getting Started

### Opening a session

When you launch Photo Reviewer you will see the start screen. Click **Open Image Folder** and select the folder containing your images. The app will scan the folder and open your session.

If you have opened folders previously they will appear as **Recent Folders** on the start screen — click any of them to reopen instantly.

### What happens on open

- The first image is displayed immediately
- Image quality scoring begins in the background
- Visual similarity hashing begins in the background (for burst detection)
- All previous tags and ratings from your last session are restored automatically

You do not need to wait for analysis to complete before you start culling.

### Resuming a session

Your tags, ratings, scores, and hashes are saved to JSON files in your image folder. Close the app at any time and reopen the folder to resume exactly where you left off.

---

## 3. The Main Interface

### Header row (top bar)

| Element | Description |
|---|---|
| **📷 Photo / Reviewer** | App identity — click Viewer to return to single image view from any overlay |
| **Viewer** | Returns to single image view. Highlighted green when active |
| **Pass** | Multi-pass culling mode toggle — see section 12 |
| **Auto→ ON/OFF** | Auto-advance on delete toggle |
| **Filename** | Current image filename — large and bold for easy reference |
| **Expose / Peak / Noise / Loupe / 1:1** | Display tools — see section 11 |
| **Rate: ☆☆☆☆☆** | Star rating for current image — click to rate |
| **Progress** | Current position e.g. 11 / 14 (78%) |
| **⛶** | Toggle fullscreen |
| **✕** | Quit |

### Workflow bar (second row)

Contains the main view buttons: Gallery, Compare, Clean Load, Detail Clean, Bursts, Species, and ? (help).

### Rename bar

Shows the current filename with an editable field. Click the name to edit, press Enter to confirm, Escape to cancel. The file type badge (JPEG, NEF etc.) is shown to the right.

### Camera bar

A thin bar showing shooting parameters for the current image: camera model, shutter speed, aperture, ISO, focal length, and date. Read from EXIF automatically.

### Canvas (main image area)

The current image displayed at fit-to-window or your chosen zoom level.

### Filmstrip

A strip of thumbnails along the bottom. The current image has a green border. Tag colours appear as stripes: green = Best, red = Delete, blue = ID, orange = Compare. A thin green progress bar above the filmstrip shows your position in the session.

### Sidebar

The right-hand panel with accordion sections: Search, Session counts, Quality scores, Histogram, Location map, Metadata, Keywords, EXIF, Duplicates.

### Footer

Action buttons: ← Prev, Next →, ★ Best, ✕ Delete, ⚑ ID, ⊞ Compare, → Topaz, ▶ Process. Keyboard shortcuts are shown beneath each button.

---

## 4. Tagging Images

Images can be tagged in three ways. Tags are mutually exclusive for Best and Delete but ID can be combined with either.

| Tag | Button | Keys | Colour |
|---|---|---|---|
| **Best** | ★ Best | B or Enter | Green |
| **Delete** | ✕ Delete | D or Backspace | Red |
| **ID** | ⚑ ID | I, Space, or + | Green (accent) |
| **Compare** | ⊞ Compare | X | Orange |

Pressing a tag key a second time removes that tag (toggle).

### Auto-advance on Delete

When **Auto→ ON** is active (green button in header), pressing Delete or D automatically advances to the next image after tagging. This is the fastest way to cull a large session. Toggle it off with the button if you prefer to stay on the deleted image.

### Undo

Press **Ctrl+Z** to undo the last tag action. Up to 50 undo steps are available per session.

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

Press **Tab** to jump to the first image with no tags of any kind. Useful for resuming a half-finished session. Wraps around to the beginning if needed. Shows a message when all images have been reviewed.

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

Use the filter pills at the top right to show only: All, ★ Best, ✕ Delete, ⚑ ID, or Untagged images. Type in the filter box to search by filename.

### Sequence separators

Images shot more than 5 minutes apart are separated by a green divider bar showing the time of the new sequence. This immediately reveals the structure of a shoot — each location or subject group appears as a distinct cluster.

### Tagging from gallery

Each cell has ★ / ✕ / ⚑ buttons so you can tag images without opening them. Click a thumbnail to jump to that image in the main viewer.

### Dynamic columns

The grid automatically adjusts column count to fill the available width as you resize the window.

Press **G** again or click **← Back** to return to single image view.

---

## 8. Burst Detection and Triage

### What is burst detection?

Photo Reviewer automatically identifies groups of images taken in rapid succession of the same subject. It uses visual similarity hashing (comparing image content) combined with a timestamp gate — images more than 30 seconds apart cannot be in the same burst even if they look similar.

This prevents false groupings of visually similar scenes shot at different times (e.g. two different tree canopy shots).

### Running burst analysis

Click **Bursts** in the workflow bar. If analysis has not yet run you will see a loading screen while it processes. Results appear in the burst triage view.

### Burst triage view

The left panel lists all detected burst groups by number of images. Click any group to review it.

Each image in the group shows:

- Thumbnail
- Quality scores (S = Sharpness, M = Motion, E = Exposure)
- **★ Best / ✕ Delete / ⚑ ID** buttons — tag individually
- **Select for batch Best** checkbox — tick multiple images then click **Mark Best & Next** to apply Best to all ticked images at once

Use **Skip →** to move to the next group without making any changes.

### What counts as a burst?

Images are grouped as a burst if:
1. Their visual content is at least 91% similar (average hash distance ≤ 6)
2. They were shot within 30 seconds of each other

---

## 9. Quality Scoring

Photo Reviewer calculates three quality metrics for each image:

| Score | What it measures | Range |
|---|---|---|
| **Sharpness** | Centre-weighted edge sharpness (Laplacian) | 0–100 |
| **Motion blur** | Gradient isotropy — motion-blurred images have directional gradients | 0–100 |
| **Exposure** | Penalty for clipped highlights or blocked shadows | 0–100 |

A **composite score** combines all three. Scores are shown in the sidebar and on gallery/burst thumbnails.

### Clean Photo Load

Click **Clean Load** to score all images using the embedded JPEG thumbnail (fast — uses the preview the camera bakes into every RAW file). Takes a few seconds for a typical session.

### Detail Photo Clean

Click **Detail Clean** for higher accuracy scoring using a half-resolution decode of the actual RAW data. Slower but gives more accurate sharpness readings, especially for images with busy backgrounds.

### Threshold

In the scoring views, images with a composite score below the **Threshold** value get a red border; above get a green border. Adjust the threshold to match your standards. After 10 tagging actions the app analyses your Best vs Delete score history and suggests a calibrated threshold in the title bar.

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

### Expose (H key)

Cycles through three display modes:

- **Normal** — standard display
- **☀ Shadows** — lifts dark tones so you can see if shadow detail is recoverable. Useful for backlit subjects
- **◐ Highlights** — crushes midtones and highlights clipped pixels in red. Shows exactly where highlight detail is lost

These are display-only — no changes are made to your files.

### Peak (F key)

Focus peaking overlay. Highlights the sharpest edges in the image in red, showing exactly where focus landed. Particularly useful for checking whether the eye of a bird or mammal is sharp. Works on the displayed image at any zoom level — more detail visible at 1:1.

### Noise (N key)

False-colour noise heat map. Blue = clean, Yellow = moderate noise, Red = heavy noise. Computed from local pixel variance in 8×8 blocks. Shows which areas of the image will benefit most from noise reduction in Topaz or DxO.

### Loupe (L key)

A 3× magnified circle that follows your cursor around the image. Shows in the bottom-right corner. Move your cursor to check focus on any part of the image without having to zoom the entire canvas. Press L or click the button again to dismiss.

### 1:1

Toggle between fit-to-window and 100% (pixel-for-pixel) zoom. At 1:1 you can drag to pan around the image.

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

The button shows the current pass and the count of images in scope (e.g. **P2·Surv 11**). Navigation and auto-advance respect the active pass.

Click the button once more to exit multi-pass mode and return to showing all images.

---

## 13. Species Identification

Click **Species** in the workflow bar to use the iNaturalist computer vision API to identify the species in your photos.

### Requirements

- An iNaturalist account (free at inaturalist.org)
- The account must be old enough to register an application (usually a few weeks)
- Internet connection

### How it works

1. Click Species and follow the setup prompts to enter your iNaturalist credentials
2. Choose the scope: all images or Best-tagged only
3. The app sends each image to the iNaturalist API and returns up to 3 species suggestions with confidence percentages
4. Click a suggestion to pre-fill the rename field with that species name

Images with GPS coordinates get more accurate results as iNaturalist uses location to refine suggestions.

---

## 14. Metadata and Keywords

### Metadata fields

The **Metadata** section in the sidebar has four editable fields:

| Field | Description |
|---|---|
| **Species** | Pre-filled from species ID if run |
| **Caption** | Free-text description |
| **Copyright** | Your copyright notice |
| **Location** | Location name |

Fields auto-save when you click away from them. All data is written to the session JSON and to XMP sidecar files for Lightroom/Capture One compatibility.

The **Apply copyright to all Best** button copies the current copyright field to every Best-tagged image in the session — useful for batch copyright application.

### Keywords

The **Keywords** section shows your custom keyword palette. Click any keyword to toggle it on or off for the current image. Add new keywords using the text entry field at the bottom. Keywords are saved to your app settings and persist across sessions.

Keywords are written to XMP sidecar files as `dc:subject` tags, which Lightroom and Capture One read natively.

---

## 15. Folder Tree

The left panel shows sibling folders of your current session folder — useful for switching between shoot days without reopening the app.

- Click any folder to switch to it instantly (your current tags are saved first)
- Click **◀** in the folder panel header to collapse it and give more space to the image
- When collapsed a green **▶ Folders** button appears — click it to expand again

The panel auto-collapses if your current folder has fewer than 3 siblings.

---

## 16. Send to Topaz

Click **→ Topaz** in the footer to send images to Topaz Photo AI for noise reduction or sharpening.

On first use you will be asked to locate the Topaz Photo AI executable. The path is saved for future sessions.

You can send:
- **Current image only** — click Yes in the prompt
- **All Best-tagged images** — click No in the prompt

Topaz opens with the selected files. Photo Reviewer remains open in the background.

---

## 17. XMP Sidecars

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

You do not need to do anything to trigger XMP writing — it happens automatically on every tag action.

---

## 18. Backup Warning

If Photo Reviewer detects that your image folder is on a removable drive (SD card, CFexpress, external USB drive) it shows an amber warning banner:

> ⚠ Source folder is on a removable drive — back up your images before marking any as Delete

This is a reminder that marking images as Delete and then removing the card without backing up first risks data loss. Click **I've backed up** to dismiss the banner for the current session.

**Always copy your images to at least one internal or NAS location before culling.**

---

## 19. Keyboard Shortcuts

### Navigation

| Key | Action |
|---|---|
| ← / → | Previous / Next image |
| Numpad 4 / 6 | Previous / Next image |
| Tab | Jump to next unreviewed image |
| G | Open / close gallery |
| Z | Toggle 1:1 zoom |
| Scroll wheel | Zoom in / out |
| Drag | Pan when zoomed |
| Escape | Reset zoom to fit |

### Tagging

| Key | Action |
|---|---|
| B or Enter | Tag as Best |
| D or Backspace | Tag as Delete (auto-advances if Auto→ ON) |
| I, Space, or + | Tag as ID |
| Ctrl + Z | Undo last tag |
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
| H | Cycle exposure inspect (normal / shadows / highlights) |
| L | Toggle loupe magnifier |
| N | Toggle noise heat map |

### Multi-pass

| Control | Action |
|---|---|
| Pass button | Cycle pass: All → Survivors → Best → off |
| Auto→ button | Toggle auto-advance on Delete |

### Other

| Key | Action |
|---|---|
| ? | Show / hide keyboard shortcuts overlay |
| Q | Quit |

---

## 20. Supported File Formats

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

## 21. Session Data and Caching

Photo Reviewer saves several files in your image folder:

| File | Contents |
|---|---|
| `photo_reviewer_status.json` | All tags, ratings, metadata, keywords |
| `photo_reviewer_hashes.json` | Visual similarity hashes for burst detection |
| `photo_reviewer_scores.json` | Quality scores for all images |

These files are small (typically a few KB) and allow sessions to be resumed instantly. They are not required and can be deleted if you want to start fresh — the app will regenerate them.

App-wide settings (Topaz path, keyword palette, recent folders) are stored in:

| File | Location |
|---|---|
| `photo_reviewer_settings.json` | Same folder as the app executable |
| `photo_reviewer_recent.json` | Same folder as the app executable |

---

## 22. Tips for Wildlife Photographers

### Recommended workflow

1. **Copy from card to hard drive first** — always before opening Photo Reviewer
2. **Open the folder** — app loads and begins analysing in background
3. **First pass** (Pass 1) — navigate quickly, mark obvious deletes. Use auto-advance on Delete for speed. Focus on: is the subject sharp, is the exposure usable, is this a duplicate of a better frame?
4. **Score** — run Clean Load to score all images. Sort gallery by score to see your weakest shots
5. **Burst triage** — click Bursts, step through groups, pick the sharpest frame from each rapid sequence
6. **Second pass** (Pass 2) — switch to Pass 2 (Survivors). Now pick your Best shots from what remains. This is your main selection pass
7. **Compare** — use compare mode to choose between similar shots
8. **Rate** — apply star ratings to your keepers: 5 stars for portfolio, 4 for social, 3 for archive
9. **Metadata** — add species, location, copyright while images are fresh in mind. Apply copyright to all Best with one click
10. **Send to Topaz** — send Best-tagged high-ISO shots for noise reduction before editing
11. **Open in Lightroom** — your tags, ratings, and keywords are already written as XMP — no re-tagging needed

### Focus checking

For birds and mammals, eye sharpness is everything. Use **Loupe (L)** to check eyes without zooming the whole canvas — move your cursor over the subject's eye and the loupe shows a 3× crop instantly.

### Exposure recovery

Backlit wildlife often looks silhouetted in the standard view. Press **H** once to activate shadow boost — if the subject detail appears in lifted shadows, the shot may be recoverable in editing. Press H again for highlight inspection to check sky clipping.

### High ISO noise

Press **N** to see which images have the worst noise before deciding whether noise reduction is worthwhile. Shots where noise is concentrated in the background but the subject is clean are often worth processing. Shots where the subject itself is heavily noisy may not be worth the effort.

### Burst settings

The app detects bursts using visual similarity with a 30-second time gate. If you shoot the same species in different positions throughout a session and they are being grouped as bursts, this means the images are visually similar enough to trigger the threshold. This is working as intended — use the burst triage view to quickly pick the best frame from each group.

---

*Photo Reviewer — built for wildlife photographers who need to cull fast and accurately.*
