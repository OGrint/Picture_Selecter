# Settings

Most settings are changed in the app and remembered automatically. A few have no button and are set by editing the settings file. Close the app first, or it may write its own copy back when it closes.

## Where they live

| File | What |
|---|---|
| `%APPDATA%\PhotoReviewer\photo_reviewer_settings.json` | App settings (below) |
| `%APPDATA%\PhotoReviewer\import_settings.json` | Card import setups (below) |

`%APPDATA%` is usually `C:\Users\<you>\AppData\Roaming`. Both files are plain JSON. A missing key means "use the default", so you only ever need to add the keys you want to change.

Example:

```json
{
  "eye_check": true,
  "graphics_acceleration": true,
  "auto_advance": 1,
  "best_template": "{species}_{date}_{location}_{n}"
}
```

## App settings (`photo_reviewer_settings.json`)

### Set only in the file

| Key | Default | Meaning |
|---|---|---|
| `graphics_acceleration` | `true` | Run the AI models on the Intel graphics chip when there is one. `false` keeps everything on the CPU. The environment variable `PHOTO_REVIEWER_GPU=0` does the same for one run. |
| `eye_check` | `true` | Find the animal's eye and compare its sharpness within bursts. `false` turns the eye pass off completely (no model loaded). |
| `raw_clip_external` | `"viewed"` | Raw highlight clipping reads each whole NEF. On an external (USB) drive that's about a second of drive time per photo, so there it's done only for the photo on screen and Best-tagged photos. `"all"` reads every frame there too, which is slower and competes with the viewer. On the internal drive, every frame is always read. |
| `detect_animals` | `true` | Run MegaDetector. `false` turns off the animal-based suggestions, sharpness on the animal, raw clipping on the animal (the frame figure is still given) and the eye check, which needs the animal. |
| `af_grid_width`, `af_grid_height` | not set | Correct the position of the drawn AF point (P key) if it is consistently offset for your camera. Viewfinder AF positions are worked out from the AF grid; these override its size. |
| `topaz_path` | `C:\Program Files\Topaz Labs LLC\Topaz Photo AI\Topaz Photo AI.exe` | The Topaz program used by Send to Topaz. The app asks for it if it isn't there, and saves your choice here. |

### Remembered from the app

| Key | Default | Set by |
|---|---|---|
| `auto_advance` | `1` | The **Auto→** button: `0` off, `1` after Delete, `2` after Best and Delete |
| `best_rename` | `false` | The "rename Best copies" tick box in the Process dialog |
| `best_template` | `{species}_{date}_{location}_{n}` | The name pattern in the Process dialog (see below) |
| `name_whole_burst` | `true` | "Apply to the whole burst" in the NAME FOR BEST panel |
| `keywords` | `[]` | The keyword palette |
| `recent_locations` | `[]` | Last 15 locations used in NAME FOR BEST |
| `folder_regions` | `{}` | The country you chose for a folder's region hint: `{folder path: ISO code}` |
| `sighting_gap` | `180` | Sightings view: seconds without photos that start a new sighting. One of 60, 180, 300, 600, 1800, 3600. |
| `sightings_show_pick` | `true` | Sightings view: show the app's predicted pick ◆ |
| `last_search` | `""` | The last AI search typed |
| `collect_parent`, `collect_dest` | `""` | Collect Best folders: the folder last searched and the collection folder |
| `collect_identify` | `true` | Collect Best folders: identify the species of photos the app hasn't named |
| `species_list_parent` | `""` | Species list from archive: the folder last read (the list is saved in its *Species list* folder) |
| `species_list_per_guess` | `"7"` | Species list: photos copied to *To clarify* per best guess per country (`"1"`, `"3"`, `"7"`, `"10"`, `"All"`) |
| `species_list_sure` | `"80%"` | Species list: how probable an AI ID must be to count as certain |
| `species_list_countries` | `{}` | Species list: the country you set per folder (`""` = not known) |
| `library_roots` | `[]` | AI search / ID inbox library folders: a list of `{"path", "volume", "label"}`. `volume` is the drive's serial number, used to find an external drive again when Windows gives it a different letter. |
| `library_root` | — | The first library folder (kept for older versions) |
| `import_dest`, `import_backup`, `import_verify` | — | From older versions; copied into the Everyday import setup once, then no longer used |

### Best name pattern

`best_template` builds the file name of each Best copy. Placeholders:

| Placeholder | Becomes |
|---|---|
| `{species}` | Species you set or confirmed, else the suggestion for the burst, else `Unidentified` |
| `{date}` | Date you set, else the capture date (`2026-06-02`) |
| `{location}` | Location you set, else the folder's label (folder name without the leading date) |
| `{time}` | Capture time (`071530`) |
| `{orig}` | Original file name without extension (`DSC_1234`) |
| `{n}` | Running number `001`, `002`… per species / date / location, never reusing a name already in `Best\` |

Spaces become `-`, characters Windows doesn't allow are removed, and doubled separators are collapsed. A RAW+JPEG pair gets the same name. If there's no `{n}` and a name is already taken, `_001` etc. is added.

## Import setups (`import_settings.json`)

Written by the import window. You don't normally need to edit it.

```json
{
  "preset": "Holiday",
  "verify": true,
  "open_early": true,
  "presets": {
    "Everyday": {"dest": "C:\\Users\\me\\Pictures", "backup": "", "year_folder": true,  "group": "day"},
    "Holiday":  {"dest": "E:\\Photos",            "backup": "", "year_folder": false, "group": "week"}
  }
}
```

| Key | Meaning |
|---|---|
| `preset` | The setup used last: `Everyday` or `Holiday` |
| `verify` | Check every copy against the card before counting it as imported |
| `open_early` | Open the first finished folder for culling while the rest copies |
| `presets.*.dest` | Destination folder |
| `presets.*.backup` | Optional second copy (empty = no backup) |
| `presets.*.year_folder` | Put folders inside a year folder (`2026\…`) |
| `presets.*.group` | `day`, `week`, `month` or `one` (single folder) |

Week folders are named by the dates they cover (`2026-06-01 to 06-07 Kenya`), months as `2026-06 Kenya`.

The record of files already imported is a separate file: see [Data files](data-files.md#per-user-files).
