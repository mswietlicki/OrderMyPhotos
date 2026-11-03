# Legacy: what OrderMyPhotos did before v2

This file records the first two versions, now kept read-only in [`archive/`](../archive/) and under the git tag `legacy-final` (commit `3ae0f55`). Figures about the library come from the OneDrive manifest on Mimir (`/srv/mimir/onedrive/manifests/onedrive-20261102-223113.lsjson.zst`, read 2026-11-02).

## v0: date sorter (C#, 2013–2014)

[`archive/dotnet/OrderMyPhotos/Program.cs`](../archive/dotnet/OrderMyPhotos/Program.cs): a .NET Framework 4.5.1 console app using ExifLib 1.4.7.

- Takes a directory and walks it recursively for `.jpg`, `.jpeg` and `.mp4`.
- Reads EXIF `DateTimeDigitized`, falling back to the file's `LastWriteTime` when it's missing, which is always the case for MP4.
- Moves every file into `<root>/yyyy-MM-dd/` and renames it to `yyyy-MM-dd HH.mm.ss.ext`, adding ` (n)` on a name collision.

**Footprint today:** 11,601 media files in OneDrive still have names like `2016-07-14 12.30.05.jpg`.

**Problems worth remembering:**
- It used `DateTimeDigitized` instead of `DateTimeOriginal`, and the file's mtime for videos. After a copy or sync, mtime is the copy time.
- The extension regex `\.jpg|\.jpeg|\.mp4` is unanchored and case-sensitive, so `.JPG` was skipped.
- It had no dry run and no log.
- Renaming threw away information in the original names: the camera prefix (`DSC_`, `P`, `IMG_`), burst suffixes and WhatsApp markers.

## v1: AI scripts (PowerShell 7 + Ollama, May 2026)

Three scripts in [`archive/powershell/`](../archive/powershell/). They ran on the Windows workstation against Ollama at `localhost:11434` and worked directly in the OneDrive folder (`%USERPROFILE%\OneDrive\Pictures\Albums\…`), so every change synced straight to the cloud.

### `find-bursts.ps1`: burst grouping and culling

1. **Order:** exiftool `DateTimeOriginal`, falling back to the file name.
2. **Grouping:** compares each photo only with the previous one. Both are squashed to 400×400 (aspect ratio ignored) and compared with ImageMagick `compare -metric RMSE`. Normalized RMSE ≤ 0.30 means the same burst. Runs of at least 2 photos form a group.
3. **Sharpness:** the standard deviation of a Laplacian over a 400 px grayscale copy. Shots below 30% of the group's best score are dropped.
4. **How many to keep:** 1 for groups of ≤ 3, 2 for ≤ 6, otherwise 3.
5. **Choosing:** a single-elimination tournament. Each match sends both photos at 512 px to `qwen3-vl` with a rubric (framing, lighting, faces, smiles, decisive moment) and "Reply with ONLY 1 or 2". The first `1`/`2` in the reply wins. On any error, photo A wins.
6. **Output:** losers move to `<album>/_archived/`, with `burst_<first>.json` recording the kept files, the archived files and the model's reasoning. Supports `-WhatIf`.

**Footprint today:** it ran over **575 of the 632 albums** under `Pictures/Albums` between 2026-05-21 and 2026-06-04. It moved **17,251 photos (117.2 GiB)** into `_archived/` folders and left **11,868 burst records**. Those photos are still on OneDrive; nothing was deleted.

### `exif-ai.ps1`: title, description and tags

1. Resizes to 1024 px with `System.Drawing`.
2. Reads EXIF with exiftool. GPS is **reverse-geocoded online through Nominatim** (OpenStreetMap), cached at 3 decimals and rate-limited to 1 request/s.
3. Skips photos that already have a `Description`.
4. Runs a three-turn chat with `qwen3-vl`: a description (2–3 sentences, ≤ 80 words), then a title (2–4 words), then 10 single-word tags as a JSON list. The date/time and location are injected into the prompt.
5. Writes `XMP-dc:Description`, `XMP-dc:Title` and `XMP-dc:Subject` with exiftool into a `tmp.jpg` copy in the working directory, then copies it over the original.
6. **Rolling album context:** after each photo, a second model (`qwen2.5:3b`) rewrites a 1–2 sentence album summary from the new caption. The summary goes into the next photo's system prompt and is saved to `<album>/_trip-context.txt`. An optional hand-written `<album>/context.txt` is added too.
7. On error it writes `error_log_<name>.json` with the whole conversation, including the base64 image.

**Footprint today:** `_trip-context.txt` exists in **5 albums** (from 1994, 2003, 2004, 2024 and 2025), and there is one `context.txt`.

**A later caption run went much further.** **592 albums** (most from 2015–2022) have a machine-written `DESCRIPTION.md`, dated 2026-05 (477) and 2026-06 (115): a title plus a few paragraphs about the album in the same style ("serene", "tranquil", "encapsulates the essence of…"). The script that wrote them isn't in this repo. On the sample album (2026-11-03), all 50 photos left in the album carry `XMP-dc:Title`, `Description` and `Subject` in v1's style, while the 21 photos in `_archived/` carry none, so the run captioned what was left after culling. v2 treats these fields as machine-written (provenance `v1`), so it may overwrite them.

### `exif-fill.ps1`: first prototype

This was the first try: one hard-coded photo, `llama3.2-vision`, a hand-typed location string, and full-resolution base64. It was superseded by `exif-ai.ps1`.

## What went wrong

| Area | Problem | Effect |
|---|---|---|
| Privacy | Photo GPS coordinates were sent to `nominatim.openstreetmap.org` | Location history left the machine. v2 geocodes offline. |
| Safety | Scripts edited and moved files inside the synced OneDrive folder | Every write re-uploaded the whole file at once. The only undo was the burst JSON records. |
| Orientation | `System.Drawing` doesn't apply the EXIF orientation | Portrait photos reached the VLM sideways. |
| Thinking mode | `think = $false/$true` was put inside Ollama `options`, which ignores it (`think` is a top-level field) | Thinking stayed on: slower, more tokens. The error logs show `thinking` text on every turn. |
| Robustness | The context model `qwen2.5:3b` wasn't pulled (HTTP 404), and incomplete responses were retried 5 times and then thrown | 3 logged failures in one 2024 album. Each one happened *after* the metadata write, so the file changed even though the run "failed". |
| Logs | The error logs embedded the image as base64 (5.6 MB each), and they plus `tmp.jpg` were committed | Private photos ended up in the public repo's history. They were deleted from the tree in a later commit but stayed in history until it was rewritten on 2026-11-02 (decisions.md P15). |
| Burst grouping | It compared each photo only with the previous one, using squashed 400 px RMSE and a 0.30 threshold, and chained groups (A≈B, B≈C, A≉C) | A scene re-shot after other photos in between was never grouped. Exposure changes or a small camera move split real bursts. Different scenes with a similar colour layout joined. Groups grew long. |
| Bad photos | At least one frame of every burst was always kept, and photos with no similar shot were never judged | A burst of only blurred frames kept its least-bad one, and accidental single shots stayed. |
| Sharpness | The global Laplacian std at 400 px rewards noise and texture and punishes intentional bokeh | Good portraits with blurred backgrounds scored low. |
| Choosing | One VLM match per pair, at 512 px, with no position swap. Failures defaulted to "A wins", and the keep count was a fixed rule | Noisy, order-biased picks. At 512 px the model couldn't judge eyes or focus. |
| Captions | Raw timestamps went into the prompt, so captions repeated them ("Evening timestamp (2024:10:26 19:22:34) indicates…") | Noise in every description. |
| Tags | 10 single-word tags with no vocabulary. Colors and generic words ("blue", "brand", "product", "casual") got in. Tags of 2 characters or fewer were filtered out | Poor search value. Multi-word concepts ("ski touring") were split up. |
| Album context | The rolling summary was updated after every photo | It drifted toward whatever came last: a trip's summary turned into a description of one supermarket product shelf. It biased later captions and forced strictly serial processing. |
| Throughput | One photo at a time, three model turns per photo, Windows-only | Fine for one album, but impractical for 175k photos. |
| Scope | Only `.jpg/.jpeg/.png` | HEIC, MPO, CR2 and videos were ignored. |

## What worked and carries over

- **Archive, don't delete,** with a per-group JSON record of what was kept and why. v2 keeps the idea, but the archive lives outside the album and nothing moves until it has been reviewed.
- **Sharpness pre-filter before the VLM:** cheap signals first, the expensive model only for close calls.
- **Rubric for "best shot":** framing, lighting, faces visible and sharp, natural expression, decisive moment. It's still the right checklist.
- **Album context helps captions:** the folder name, place and a hand-written `context.txt`. v2 computes it once per album or event instead of rolling it.
- **Skip what a human already wrote:** v2 also marks its own writes, so they can be told apart from the user's.
- **XMP `dc:title` / `dc:description` / `dc:subject`** are still the core fields to write.

## Inputs v2 must take over from v1

v2 redoes all of it: grouping, culling, captions and tags, over every album. v1's results are **suggestions only** (user decision, 2026-11-02).

- **The `_archived/` folders** (17,251 photos) are put back into the candidate pool as if v1 never ran. Each photo is grouped and scored at its original album position.
- **The 11,868 `burst_*.json` records** show up as a "v1 kept this" hint in the review UI. They also serve as a baseline for measuring how far v2 differs. They never decide anything on their own.
- **The `_trip-context.txt`, `DESCRIPTION.md` and `context.txt` files** are album context. `context.txt` is user-written, so it's trusted. The other two are machine-written, so they're only hints, and v2's captions don't use them.
- **XMP title, description and subject written by v1** in albums with a `_trip-context.txt` or `DESCRIPTION.md` (about 590 albums; the five `_trip-context.txt` albums seem to have one too) count as "machine-written, may overwrite", unlike user-written metadata.
