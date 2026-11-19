# Photo Reviewer documentation

Photo Reviewer is an offline culling tool for wildlife and bird photographers. It sits between the memory card and your editor: import, tag Best / Delete / ID, check sharpness and exposure, name the keepers by species, and hand them on. It works with Topaz, darktable and Lightroom.

## Using the app

| Page | What it covers |
|---|---|
| [Installation and running](installation.md) | Running from Python (conda), the model files, a desktop shortcut, building the Windows installer |
| [Quick start](quick-start.md) | One shoot from card to `Best/`, step by step |
| [User guide](../Photo_Reviewer_User_Guide.md) | Every feature in detail: tagging, views, sightings, scoring, eye check, raw clipping, ID Review, AI search, import, folders, XMP |
| [Keyboard shortcuts](keyboard-shortcuts.md) | Every key, in every view |
| [Troubleshooting](troubleshooting.md) | Problems and what to do about them |

## Reference

| Page | What it covers |
|---|---|
| [Settings](settings.md) | Every key in the settings files, with defaults |
| [Data files](data-files.md) | Every file the app writes, in the photo folder and in `%APPDATA%`, and which are safe to delete |
| [AI models](ai-models.md) | The models: what each does, size, licence, accuracy notes, graphics-chip acceleration, how to rebuild them |

## Working on the code

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | Modules, threads and background passes, how data flows, rules for changes |
| [Testing](testing.md) | Unit tests, what they cover, how to check a change in the real app |

## Ground rules the app follows

- **Nothing leaves your computer.** All the analysis is local. The only network use is a one-off download of a country's species list from iNaturalist when you use the region hint (see [AI models](ai-models.md#region-hint-data)).
- **Nothing is tagged for you.** Animal detection, keeper prediction, raw clipping and the eye check only make suggestions. Frames you leave untagged stay untagged.
- **Your session files stay compatible.** New versions only add keys to the files in your photo folders. Older sessions open as they are.
- **RAW files are never modified.** Tags, ratings and keywords go into `.xmp` sidecars and the session file.
