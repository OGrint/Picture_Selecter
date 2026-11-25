# Quick start: one shoot from card to Best

This walks through a typical day's shoot. Each step links to the [user guide](../Photo_Reviewer_User_Guide.md) for detail.

## 1. Import the card

On the start screen click **📥 Import from card…** (or open a folder you've already copied).

- **Setup:** pick **Everyday** (the laptop drive) or **Holiday** (e.g. the external drive). Each remembers its own destination, optional backup folder, whether to add a year folder, and how to group: per day, per week, per month or one folder.
- **Labels while it copies:** each folder gets a box for a label (e.g. *Minsmere*). Copying starts straight away, so type while it copies. The label is added to the folder name when that folder is finished.
- **Start early:** the first finished folder opens for culling while the rest is still copying.
- **No double copies:** files already imported are skipped, even if you import the card again with different grouping.

Result, for example: `D:\Photos\2026\2026-06-02 Minsmere\DSC_1234.NEF`.

## 2. Let it analyse (keep working meanwhile)

Opening a folder starts background work, shown at the top of the window. You can cull straight away.

| Pass | Gives you |
|---|---|
| Preparing folder | Capture times, focus points, burst groups |
| Analysing | Sharpness at the focus point, exposure, animal detection, keeper features, search index |
| Species | Species suggestions per burst |
| Raw clipping | Real blown highlights from the RAW data (photo on screen first) |
| Eye check | Where the eye is and which frame in each burst has the sharpest eye (photo on screen first) |

Each folder is analysed once; reopening it is instant.

## 3. Cull

The fastest way through a shoot is by **sighting**: press **V**.

- **What a sighting is:** bursts of the same animal are grouped into one sighting, by species and time gap (3 minutes by default).
- **Tag:** tag frames with **B** (Best), **D** (Delete), **I** (ID: identify later). Untagged frames stay untagged.
- **Species:** press **N** to set the species for the whole sighting.
- **Next sighting:** press **Tab** to mark it done and go to the next one.

Or go frame by frame in the main viewer with **←/→**. In either, these help decide:

- **Sidebar:** sharpness at the focus point, *Raw: …* (blown highlights from the sensor data) and *Eye: sharpest in this burst / softer than DSC_… / not found*.
- **⚠ suggestions:** no animal, focus missed, cut off, blown, soft eye. They're only suggestions.
- **Display tools:** **E** jumps to 100 % on the eye, **H** shows where the RAW really clipped, **F** focus peaking, **L** loupe, **Z** 100 % zoom, **P** focus point and animal boxes.
- **K** jumps to the predicted keeper of the burst, once the app has learned your picks.

For bigger overviews: **G** gallery, **⊹ Bursts**, **⚡ Clean Load** (lowest scorers first), **C** compare up to 4 frames side by side.

## 4. Name the keepers

In the **NAME FOR BEST** panel the species (suggested, or press **N** to accept), date and location (from the folder label) are filled in. Change them if needed. They apply to the whole burst. The preview shows the file name the Best copy will get, e.g. `Grey-Heron_2026-06-02_Minsmere_001.NEF`.

## 5. Identify the unsure ones (any time later)

**🔎 ID Review** goes through ID-tagged frames sighting by sighting, with species suggestions. **1–5** picks a suggestion, **U** marks it unsure, **X** not identifiable. The **ID inbox** lists folders that still have frames to identify, across all your library folders.

## 6. Process

Press **▶ Process**:

- **Best** frames are copied to `Best\` with their XMP sidecars. If you tick renaming, every planned name is listed: untick any photo whose copy should keep its camera name.
- **ID** frames are converted to JPEG in `ID\`.
- **Delete** frames go to the Recycle Bin.

Then take `Best\` into Topaz and darktable. Ratings, labels and keywords are already in the XMP sidecars.

## 7. Now and then: collect your Best photos

**🗂 Collect Best folders…** on the start screen copies the final Best photos of every trip under a folder, with their Topaz and darktable versions, into one library sorted by year, trip and species. Check the preview, fix trip names and species, then Copy. Run it again any time; only new photos are added.

**🦜 Species list from archive…** does the same for a species list: every photo under a folder → one photo of each species per country, plus CSV lists. Uncertain IDs go into *To clarify*; check them in the app with **Review…** (pick a guess with 1–3, type your own, or Del to remove). It's resumable: stop it whenever you like.

## 8. Later: find anything

**🔍 Search** finds photos by description across all your library folders ("kingfisher with fish", "snowy owl portrait"), with Best / stars / species / year filters. Folders on the external drive are still searchable while it's unplugged, from an offline copy with thumbnails.

## Tips

- **Rename a folder** with **✎ Rename** in the folder list, or right-click it. Tags, caches, region and history follow the new name.
- **White birds:** the camera's preview shows them blown long before they are. Trust the *Raw:* line and **H**.
- **Choosing between frames of a burst:** arrow through it pressing **E** on each to compare eyes at 100 %.
