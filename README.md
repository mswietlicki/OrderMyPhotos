OrderMyPhotos
=============

Organizes a private photo library of about 175k photos, 2008–2026, kept in OneDrive. Everything runs on a home AI server, [Mimir](https://github.com/mswietlicki/mimir) (RTX 4090 + Arc Pro B50), with local models only. The pipeline:

1. **Groups** bursts (the same scene shot several times from the same place, seconds or many minutes apart) and finds copies of the same photo across folders.
2. **Scores** every shot on sharpness on the subject, exposure, faces (eyes open, expression), aesthetics and composition.
3. **Picks** the best shot of each burst, and flags photos that are bad on their own even when nothing similar exists. You review the picks; the rest move to an archive and are deleted only once you say so.
4. **Organizes:** sorts unsorted phone photos into event albums, and sends reference photos (documents, receipts, serial labels, specs, food, notes "to remember") into category albums next to your `Pictures/Info` and `Pictures/Other` folders, with their text searchable.
5. **Describes** every photo it keeps with a title, a description and tags.
6. **Recognizes faces**: clusters them, you name the people, and the names are used in tags and captions.
7. **Writes back** to OneDrive: metadata into the files, plus server-side moves.

**Status:** v2 design, 2026-11-02. No code yet. The first test target is a sample album.

## Rules

- **Photos never leave the machine.** No cloud vision APIs, and no online geocoding. An external text-only API (e.g. OpenRouter) is allowed only where a decision record says so, and never with pixels, GPS or names.
- **The OneDrive mirror is read-only.** Results go to a database and a work area. OneDrive changes only through a reviewed *apply* step.
- **Archive before delete,** and delete only after review. Every decision is recorded with its reasons.
- **Every stage is idempotent and resumable,** keyed by file content hash and model version. A full run takes days, so it has to survive restarts.

## Docs

| Doc | What |
|---|---|
| [docs/design.md](docs/design.md) | Architecture and every pipeline stage, with what improves on v1 |
| [docs/models.md](docs/models.md) | Model shortlist per stage: sizes, licenses, which GPU |
| [docs/plan.md](docs/plan.md) | Phased plan with checkboxes |
| [docs/decisions.md](docs/decisions.md) | Decision log |
| [docs/legacy.md](docs/legacy.md) | What v0 (C#, 2013) and v1 (PowerShell + Ollama, 2026) did and left behind |

Old code lives in [`archive/`](archive/) and under the tag `legacy-final`.
