# Design (v2)

Status: **draft, 2026-11-02.** Not built yet. Model choices are in [models.md](models.md), the order of work is in [plan.md](plan.md), and the reasons behind choices are in [decisions.md](decisions.md).

## Goals

1. **Deduplicate across the whole drive.** Find exact copies, and also resized, recompressed and edited versions of the same photo saved in different directories (an album and the camera folder, WhatsApp copies, exports). Keep the best original.
2. **Cull.** Find **bursts**, meaning several shots of the same scene from the same place: a group photo retaken a few times, a landscape, a landmark. They may be seconds or many minutes apart, and are not necessarily the camera's burst mode. Pick the best shot or shots, and archive the rest. Also flag **photos that are bad on their own** (badly blurred, black, accidental) even when nothing similar exists, and archive a whole burst when every frame in it is bad. Delete only after review.
3. **Organize.** Tell **memories** apart from **reference photos**: documents, receipts, serial-number labels, specs, screens, food, notes "to remember". Split the memories in the unsorted camera streams (`Obrazy/Samsung Gallery`, `Camera Roll`, `Obrazy/Other`, `Pictures/Photos`, WhatsApp, …) into events. Propose albums named like the existing ones (`Pictures/Albums/<year>/<YYYY-MM-DD> <name>`), or add photos to an existing album when they belong to the same event. Route reference photos to **category albums**, extending the user's own `Pictures/Info/*` and `Pictures/Other/*` folders, with their text made searchable.
4. **Describe.** Give every photo that's kept a title, a description and tags that are worth searching.
5. **Recognize people.** Cluster faces library-wide, let the user name the clusters, and put the names into tags, captions and face regions.
6. **Live mode.** Run on a schedule and take new phone photos through all of the above as they arrive. The results go to a daily review queue.
7. **Write back.** Apply all of it to OneDrive through server-side moves and metadata inside the files, so OneDrive, Windows, Immich and digiKam all see it.
8. **Videos too.** They are 37% of the space. Videos get copy and junk-clip detection, captions from keyframes plus speech, albums, and optional re-encoding to save space (S12).

**Out of scope:** editing pixels (auto-crop, auto-enhance). The one lossy operation is video re-encoding, which is opt-in and checked by VMAF.

## Constraints

- **Local only.** Pixels, GPS, faces and names stay on Mimir. That rules out cloud vision APIs and online geocoding (v1 sent GPS to Nominatim). An external text-only LLM (OpenRouter) is not part of the design. If it's ever used, it gets its own decision record, and it never sees pixels, coordinates or names.
- **The mirror is read-only** ([mimir D15](https://github.com/mswietlicki/mimir/blob/main/docs/decisions.md)). `/srv/mimir/onedrive/mirror/` belongs to the hourly one-way sync. The pipeline reads it, writes to its own database and work area, and changes OneDrive only through the *apply* step. The next sync then brings the changes back down.
- **Shared GPUs.** Qwen3.8-27B normally lives on the 4090 (22.2 GB) for chat. The B50 always holds the embedding and reranker models for RAG (7.6 GiB), which leaves about 8 GiB free. Batch work borrows the 4090 through llama-swap, like any other model, so chat isn't broken silently.
- **Runs for days.** Every stage has to be resumable after a reboot, a crash or a model swap, and safe to re-run.

## The library (OneDrive manifest, 2026-11-02)

| | Files | Size |
|---|---|---|
| Photos | 173.9k | 615 GiB |
| ↳ JPEG | 171k | 609 GiB |
| ↳ PNG / HEIC / MPO / CR2 / GIF / WebP | 1,764 / 424 / 425 / 34 / 149 / 53 | ~7 GiB |
| Videos (MP4, MTS, AVI, MKV, …) | 4,786 | 359 GiB |
| **Exact duplicates** (same QuickXorHash) | photos: 10,331 redundant copies (8,274 groups); videos: 297 (281 groups) | **22.0 + 46.8 = 68.8 GiB** |

| Where | Photos | Size | Notes |
|---|---|---|---|
| `Pictures/Albums/<year>/<date name>` | 86k in 632 albums | 362 GiB | Curated by the user. v1 culled 575 of these albums |
| `Obrazy/Samsung Gallery` | 43k | 150 GiB | Phone camera stream, not sorted into albums |
| `Obrazy/Other` | 15k | 35 GiB | 6,805 of these are exact copies of files in `Pictures/Photos` |
| `Pictures/Photos` | 10.5k | 28 GiB | |
| `Obrazy/Camera Roll`, `WhatsApp Images`, `Screenshots`, … | ~10k | ~22 GiB | |
| Wallpapers, prints (`Druk`), thermal camera, invoices, backups, website | ~6k | ~12 GiB | Not personal photos. Default policy: ignore |

File names show the cameras: `YYYYMMDD_HHMMSS` (Samsung, 97k), `P…` (Panasonic/Olympus, 25k), v0's renamed `YYYY-MM-DD HH.MM.SS` (11.6k), `IMG_…` (8.8k + 4k with a `_NNN` suffix), WhatsApp `IMG-…-WA…` (3.7k), `DSC_`, `DSCF`, `IMAG`.

## Architecture

```
                 /srv/mimir/onedrive/mirror  (read-only, hourly rclone pull)
                              │ paths
 ┌────────────────────────────▼──────────────────────────────────────────────────────┐
 │ omp runner (CPU container, 20 worker processes; `omp live` on a timer)             │
 │   inventory → copies → previews → signals → groups → ranking → albums → captions   │
 │   → plan          (photos and videos; video adds ffprobe, keyframes, fingerprints) │
 │   job queue + results in SQLite (/srv/mimir/data/photos/omp.db, WAL)               │
 │   previews/crops cache (/srv/mimir/cache/photos, regenerable, not snapshotted)     │
 └───────┬───────────────────────────────┬───────────────────────────────┬───────────┘
         │ HTTP, image *paths*            │ HTTP (OpenAI API)             │
         ▼                                ▼                               │
 llama-swap (127.0.0.1:9292): owns both GPUs, swaps by declared capacity  │
   ├─ omp-vision-cuda    4090: embeddings, IQA/aesthetics, faces          │
   ├─ qwen3.8-27b(-batch) 4090: VLM judge + captions (JSON schema)        │
   ├─ omp-vision-xpu     B50: depth, faces/IQA where they run on XPU      │
   ├─ depth-anything-v2  B50 (existing)                                    │
   └─ qwen3-embedding-4b B50 (existing): tag normalisation, text search   │
                                                                          ▼
 omp web (127.0.0.1, Tailscale Serve): review picks, albums, daily digest, approve plans
 Immich (read-only external library on the final layout): browse, search, name people
 Photon (127.0.0.1): offline reverse geocoding, Polish place names
         │ approved plan (JSONL)
         ▼
 mimir-onedrive apply  (user `onedrive`, the only one holding the token)
   server-side moves by item ID, metadata uploads, hash-checked, logged
```

- **The GPU model servers are llama-swap "models"** with measured `capacities`, the same way the depth service works today ([mimir D10](https://github.com/mswietlicki/mimir/blob/main/docs/decisions.md)). When the vision server needs the 4090, llama-swap evicts Qwen3.8 and later brings it back. Nothing has to coordinate GPU use by hand. The servers mount the mirror and the cache read-only and receive file paths, not image bytes.
- **SQLite is enough.** The database holds about 200k photos, a few hundred thousand faces and several embeddings per photo (~2–4 KB each, roughly 1–2 GB in total). One runner writes, and the web UI reads and writes short transactions. Vector search on that scale is a brute-force numpy matmul, so no vector database is needed.
- **Code:** Python 3.13 and uv, one package `omp` with a CLI (`omp inventory`, `omp signals`, `omp group`, …). Each stage reads the inputs it needs from the DB and records the outputs with `(photo, stage, model, version)`. A stage only re-runs when its inputs or model version change.

## Identity and state

- **Photo identity = the content hash** (QuickXorHash, computed locally and the same one OneDrive uses), plus the size. A move or rename on OneDrive doesn't change it, rclone `--track-renames` keeps the mirror's copy, and it joins directly with the manifest's item IDs at apply time. The path is just an attribute.
- **Tables (sketch):**
  - `file` (hash, path, item_id, size, mtime, folder policy);
  - `photo` (hash, capture time and its source, device, GPS, orientation, dims, existing metadata and its provenance);
  - `signal` (hash, kind, model@version, value or vector);
  - `face` (hash, box, landmarks, embedding, attributes, person);
  - `person`;
  - `group` (kind copy/burst, members, cohesion);
  - `decision` (group, keep and archive lists, reasons, confidence, source auto/user, v1 suggestion);
  - `caption` (hash, model@version, JSON);
  - `plan_op` (op, item_id, expected hash, state);
  - `label` (every user override, which also trains the ranker).
- **Provenance of metadata:** every title, description or tag found in a file is classified as user, v1 or v2. v2 never overwrites what the user wrote. v2 marks its own writes with `XMP-xmp:CreatorTool` plus a small custom namespace holding the version, so the next run can recognize them.

## Pipeline stages

Each stage lists **v1** (what was done before) and **v2** (what changes and why).

### S0 Inventory (CPU)

- Walk the mirror, apply `policy.yaml` and hash every file. The policy maps path globs to:
  - `cull`: curated folders. These are the albums, plus `Pictures/Info/*` and `Pictures/Other/*`, which also serve as labelled examples for S3b.
  - `unsorted`: camera streams, which are culled and also sorted into event or category albums (S11).
  - `describe`: screenshots and scans, captions only.
  - `ignore`: wallpapers, backups, website files. The business invoice and scan folders are `ignore` too, but they still count as curated copies in S1 and as examples in S3b.
- Read metadata in batches with exiftool `-stay_open`: `DateTimeOriginal` + `SubSecTimeOriginal` + `OffsetTimeOriginal`, make/model/serial, lens, ISO, shutter, aperture, focal length, GPS, orientation, dimensions, burst IDs (Google/Apple), Motion Photo (Samsung `EmbeddedVideoType=MotionPhoto_Data`, Google `MicroVideo`), and the existing XMP/IPTC title, description, keywords, rating and face regions. Videos are read with `ffprobe` (S12).
- **Capture time with a recorded source:** EXIF, then the file name (`YYYYMMDD_HHMMSS`, `IMG-YYYYMMDD-WA`, v0's `YYYY-MM-DD HH.MM.SS`), then GPS time, then mtime. Sub-second time matters for ordering bursts. Clock drift between devices can be estimated later from shared events.
- **v1 traces:** `_archived/*` photos get their album path back as a logical location and become normal candidates. `burst_*.json` turns into `v1_suggestion` rows, and `context.txt` / `_trip-context.txt` into album context (trusted / hint).
- *v1:* exiftool on each file, `DateTimeOriginal` only, at the 1 s resolution its parser read, `.jpg/.jpeg/.png` only.

### S1 Exact duplicates (CPU, no AI)

- Same hash and size means the same bytes. Keep one copy, chosen by location precedence (`Pictures/Albums` > `Pictures/People|Places|Info|Other` and the business invoice/scan folders > `Pictures/Photos` > `Obrazy/Samsung Gallery` > `Camera Roll` > `Obrazy/Other` > `WhatsApp`, configurable), and archive the rest. For example, 97 phone photos also stored in `…/Faktury` keep their invoice-folder copy.
- This removes 10,628 files / 68.8 GiB (most of the space is in 297 duplicate videos) with zero risk, so it's the first thing to apply. The biggest overlap is 6,805 photos stored in both `Pictures/Photos` and `Obrazy/Other`.
- *v1:* none.

### S2 Decode and previews (CPU)

- Decode once with **EXIF orientation applied**. JPEG uses libjpeg-turbo DCT-scaled decoding for previews, and full resolution only where sharpness is measured. HEIC uses pillow-heif. MPO uses its first frame (these are stereo pairs). CR2 uses its embedded JPEG.
- Cache a 1536 px preview (for VLMs and IQA) and a 384 px thumbnail (for the UI) in the cache directory. Models that need 224–518 px resize from the preview.
- *v1:* `System.Drawing` without orientation, separately in every script, copying files to `tmp.jpg` in the working directory.

### S3 Signals (both GPUs + CPU)

Each signal is computed once per photo and stored. Photos with the `ignore` policy are skipped. The first group of signals is cheap and runs on everything:

| Signal | How | Used for |
|---|---|---|
| Near-duplicate embedding | DINOv2-B or DINOv3-L ([models.md](models.md)) | copies, bursts |
| Semantic embedding | SigLIP 2 So400m | text→image search, zero-shot labels, tag verification |
| Perceptual hash | pHash/dHash 64-bit | resized or re-encoded copies across folders (WhatsApp, prints, exports) |
| Technical + aesthetic quality | Q-ReAlign Mini, one pass giving both scores (Pro re-scores group finalists) | blur, noise, exposure, compression; composition, appeal |
| **Sharpness map** | Tenengrad / variance of Laplacian on a tile grid of the *full-resolution* luminance, after light denoising, plus gradient anisotropy for motion blur | global, subject and face sharpness; motion vs. defocus |
| Exposure | clipped highlights and shadows (%), median luminance, luminance on faces vs. background | hard filters, ranking |
| Noise | MAD noise estimate in flat tiles, ISO | ranking, IQA cross-check |
| Kind | memory / reference / media / junk, plus a category (S3b) | grouping, floor, albums, captions |
| Faces | detector + landmarks + identity embedding + attributes (models.md): box, size, eyes open L/R, smile and expression, yaw/pitch/roll, face sharpness, face-quality score | ranking, people |
| **Subjects and their actions** | every person and animal (RT-DETR boxes: person, dog, cat, horse, bird, …), the **pose** of each (ViTPose++ keypoints: 17 body points for people, the animal head for animals), **facing** the camera or away, cut by the frame edge or occluded, and an **action** label (SigLIP 2 zero-shot on the subject's crop over a verb list in English and Polish: standing, walking, running, jumping, sitting, skiing, climbing, hugging, eating, playing, …) | what happens in the frame: bursts, keep count, the moment, captions (P22) |

**Expensive signals** go only to photos where a decision depends on them: members of a burst (S4 groups first, using only the cheap signals), and singles whose cheap scores fall below a pre-screen line, which makes them candidates for the quality floor in S5:

| Signal | How | Why |
|---|---|---|
| **Depth** + **saliency** | Depth-Anything-V2-Small (on the B50 today; DA3-Small/Base to compare) + BiRefNet | Builds the **subject mask** from the near depth layer, the salient object and the faces, so sharpness is measured *where the subject is*. Bokeh is no longer penalized, and a frame where the focus landed on the background is caught. Also gives subject isolation and flags accidental shots (flat depth plus darkness: pockets, the floor) |
| Subject cut-off | subject mask touching the frame edge; faces or heads cut by the border | composition |
| Eye-region detail | face crops at full resolution for the faces that matter (largest or central) | blink and focus decisions in group shots |

- *v1:* Laplacian std at 400 px, globally, as the only signal.

### S3b Kinds: memory or reference

Not every photo is a memory of a trip or event. Many were taken to keep information. This is decided early, because grouping (S4), the quality floor (S5), captions (S7) and albums (S11) all treat the two differently.

- **Purpose** (top level):
  - `memory`: people, places, events, scenery, pets;
  - `reference`: taken to keep information;
  - `media`: memes, forwarded images, infographics, screenshots of things;
  - `junk`: accidental shots.
- **Category** for reference and media photos, from `categories.yaml`, which the user edits. Defaults follow the folders that already exist:

  | Category | Examples | Album (existing folder ✓) | Placement |
  |---|---|---|---|
  | Documents | letters, forms, contracts, certificates, driving-licence papers | `Pictures/Info/Documents` ✓ | always the category |
  | Receipts and invoices | paragony, faktury, warranty cards | `Pictures/Info/Paragony` ✓ | always the category |
  | Labels and serial numbers | nameplates, router and appliance labels, VIN, model/serial stickers | `Pictures/Info/Hardware` ✓ | always the category |
  | Specs and manuals | spec sheets, product boxes, manual pages, wiring diagrams | `Pictures/Info/Hardware` ✓ or a new `Info/Instrukcje` | always the category |
  | Screens and boards | photos of monitors, slides, whiteboards, timetables | new `Info/Ekrany` | always the category |
  | Home and repairs | pipes and wiring before the wall is closed, meter readings, measurements, damage for insurance | new `Info/Dom` | always the category |
  | To remember | parking spot, a price, where something was put, a plant or product to look up | new `Info/Notatki` | always the category |
  | Climbing topos, maps, info boards | route topos, trail maps | `Pictures/Info/Topos` ✓ | the event if shot on a trip, otherwise the category |
  | Food and drinks | dishes, menus, wine labels | `Pictures/Other/Food` ✓ | the event if shot on a trip, otherwise the category |
  | Shopping | products in a shop, price tags | new `Info/Zakupy` | the event if shot on a trip, otherwise the category |
  | Memes, funny, infographics | forwarded images | `Pictures/Other/Meme`, `Funny`, `Infographic` ✓ | always the category |
  | **Sensitive** | ID cards, passports, payment cards, medical papers | not moved: flagged, with a suggestion to move it into Personal Vault by hand | never moved automatically |

  The placement rule matters for trips. A receipt from a holiday goes to Receipts and gets an `Events/<album name>` tag, so the trip can still find it. A dinner on that holiday stays in the trip album with a `Food` tag, and the Food album lists it through Immich or a tag instead of a second copy of the file.
- **How it's classified:**
  1. **A linear probe on the SigLIP 2 embeddings,** trained on folders the user already sorted. The labelled examples come free:
     - `Pictures/Info/*` (Documents, Hardware, Paragony, Topos, …), ~240 photos;
     - `Pictures/Other/*` (Food 243, Meme 239, Funny, Infographic, Art, …);
     - the invoice and scan folders (`…/Faktury`, `Obrazy/Scan`), ~1,000;
     - `Screenshots`;
     - album photos as `memory`.

     This learns the user's own idea of each category. Zero-shot prompts in English and Polish cover categories that have no examples yet.
  2. **Context features:**
     - planarity from the depth map (documents, screens and labels are flat);
     - how much text is in the image;
     - close-up distance;
     - whether the photo is isolated in time or inside a dense event;
     - whether it was taken at home (GPS).
  3. **The VLM confirms** in its S7 JSON (`purpose`, `category`, `text_in_image`). When the probe and the VLM disagree, the photo goes to review.
- **Safety:** moving a memory out of an event album by mistake is the costly error, so `memory` wins ties. Photos already in a curated album are moved to a category only on high confidence, and only after review.
- *v1:* none. Receipts and product shots got trip captions ("… grocery shopping trip featuring …").

### S4 Grouping (CPU)

There are two kinds of group, plus events above them:

1. **Copies across directories** (the same photo saved more than once): a library-wide nearest-neighbour search on the near-duplicate embeddings. That's 175k × 175k cosine, brute force in chunks on the GPU, done in seconds. It isn't limited by time or folder. Candidates are confirmed by several things:
   - The same capture time, when both files have EXIF; or a date in the file name (WhatsApp strips EXIF).
   - pHash distance (resized or recompressed copies).
   - A **geometric check** with local feature matching for crops, rotations and filtered edits: SIFT + RANSAC on the CPU, or ALIKED + LightGlue if SIFT misses filtered edits. The homography tells whether B is a crop of A.

   Copies are classified as byte-identical (from S1), resized/recompressed, or edited (cropped, filtered, rotated). The default is to keep the best original: highest resolution, EXIF intact, least compressed, in the most curated location. **An edit is kept next to its original by default,** because an edit is a deliberate choice, and the user can flip that policy.
2. **Burst**, in the user's sense: *the same scene shot several times from the same place.* Examples are a group photo retaken until everyone looks right, a landscape or landmark shot again a few minutes later, or the same moment from two phones. A burst may span many minutes, so time alone can't define it. Several kinds of evidence work together:
   - **Scene similarity**: the near-duplicate embedding cosine, the main signal.
   - **Same place**: GPS within ~100 m when both photos have it. Without GPS, the same event is enough.
   - **Time as a soft prior, not a cut-off.** Candidates are paired within one event, up to a configurable window (default ~60 min). The similarity a pair needs rises with the gap between them: a 2-second pair joins easily, while a 40-minute pair has to look clearly like the same shot.
   - **Same viewpoint**, for long gaps: a geometric check (SIFT + RANSAC inliers) confirms that two landscape or landmark shots are taken from roughly the same spot, rather than being two different views of the same mountain.
   - **Same people**, for group photos: the identity sets of the faces overlap, against the same background.
   - **Camera-burst hints** seed and tighten groups but are never required: sub-second sequences and burst naming or metadata. Samsung writes no burst tag, only `YYYYMMDD_HHMMSS_NNN.jpg` names. Google writes `XMP-GCamera:BurstID`/`BurstPrimary`, Apple `BurstUUID`, and Huawei/Pixel `…_BURST001_COVER`.

   **Reference photos follow a stricter rule.** Pages of one document, two receipts or two labels look alike to an image embedding, yet each is unique. For text-heavy kinds, two photos are the same burst only if their **text** matches too (OCR or `text_in_image` similarity). Different pages instead form a **document set**: kept, ordered, and stored together. Retakes of the same page or label are a real burst, and the most legible one wins.

   **Clustering:** each event's pairs form a similarity graph, which is clustered with average linkage plus a maximum diameter, so every member resembles the group and not just its neighbour. This stops chains like A≈B≈C where A≉C. Photos don't have to be consecutive: a scene re-shot after other photos in between still joins its burst.
3. **Events** feed S11 (albums), caption context and the burst search window: time-gap segmentation that adapts to the local shooting rate, plus GPS distance.

Copies are resolved first, so a burst never counts the same frame twice. Every photo outside a group is a **single**, and S5 still judges it on its own. The thresholds (the similarity curve over time, distance, diameter) are calibrated on the sample album's labels, and each group records how strongly it holds together, so the review UI can show loose groups first.

- *v1:* RMSE on 400×400 squashed thumbnails, comparing only neighbours with threshold 0.30. One different photo in between split a burst, exposure changes or a small camera move broke it, and two different scenes with a similar colour layout could join.

### S5 Ranking and selection (CPU + VLM for close calls)

Two questions are asked of every photo. **Is it the best of its burst?** That's relative, steps 1–7 below. **Is it worth keeping at all?** That's absolute: the *quality floor*, step 8, which also covers singles. v1 asked only the first question, and always kept at least one frame of each burst, however bad.

For each burst:

1. **Relative hard rejects.** A frame is rejected when its subject sharpness is below about half the group's best; when a main face has its eyes closed and another frame has them open; when it's heavily clipped; or when it's accidental.
2. **Feature score.** A weighted sum of features normalized within the group: subject sharpness, face score (eyes, expression, gaze, face sharpness), technical IQA, aesthetics, exposure. The weights start hand-set and are then **fitted to the user's own choices**: a pairwise logistic / Bradley–Terry model on feature differences, trained from review overrides. The ranker becomes personal over time.
3. **The moment** (P22). What the people and animals are doing is often why a photo is worth keeping, so it gets its own features: the action caught at its peak within the burst (the top of a jump, the widest stride, the throw), the subject facing the camera, eyes open, nobody cut off by the edge, and the subject sharp despite moving. Their weights come from the user's labels, like the rest of the score.
4. **VLM judge, only for close calls** (the top candidates within a margin ε). Qwen3.8-27B (`:instruct`, thinking off) gets two photos at 1024–1536 px. For group shots it also gets their face crops side by side, and for action shots the crops of the main subject. The rubric asks first what each person and animal is doing in each frame, then which frame caught the better moment. It answers with a JSON rubric (per criterion which photo is better, plus the reasons), and the overall verdict is read from the logprobs of the `1`/`2` answer token. **Every pair is asked in both orders** and the two probabilities are averaged, so position bias cancels out. A near-even result is a tie. Groups with more than 2 contenders use a round-robin over the top 4 (≤ 12 calls), aggregated with Bradley–Terry. A parse failure means "no verdict", never "A wins".
5. **How many to keep.** The best frame is kept unless it fails the quality floor (step 8), in which case the **whole burst can be archived**. More frames are kept only if they're not clearly worse than the best **and** they're visibly different: by the whole-frame embedding, or **because a main subject does something else** (P22). One person walking and another standing still, a dog before and after the jump, someone facing the camera and then turning away: those are different photos even when the frames are 0.97 alike as a whole. The subject comparison uses the pose (keypoints normalized to the subject's box), the action label, facing, and who is in the frame (someone entered or left). This is MMR-style diversity selection with no fixed cap (P20), and an album can override it, for example for sports or kids. For long, slow bursts (a landmark shot again and again over half an hour), the diversity test matters more than for a 2-second burst, so different light or compositions survive.
6. **Decision record:** kept and archived frames, per-feature scores, plain-language reasons ("eyes closed: <name>", "subject soft: 38% of best"), confidence, the v1 suggestion and model versions.
7. **Confidence tiers.** `auto` covers exact copies and bursts with a clear winner. `review` covers everything else. Nothing is applied until the user approves it; `auto` just makes bulk approval quick.
8. **Quality floor: bad on its own.** A photo is proposed for archiving even when nothing similar exists, if it clearly fails on its own:
   - **badly blurred:** subject sharpness far below what the camera and ISO normally reach, motion or missed focus, not bokeh;
   - **nearly black or white:** blown or crushed with no recoverable subject;
   - **accidental:** pocket or floor shots, a finger over the lens, the ceiling, flat depth plus darkness, nothing recognizable;
   - **failed:** a heavy shake smear, or a lens flare washing out everything;
   - **low on every signal at once:** very low technical *and* aesthetic scores.

   The cheap signals pre-screen, the expensive ones confirm, and for anything borderline the VLM is asked "worth keeping, and why not?" with a JSON verdict and reasons. The floor is absolute, calibrated per camera and era: a 2009 compact photo isn't judged against a 2024 phone.

   **Reference photos are judged on legibility, not beauty.** An ugly but readable receipt passes, and a label lost to glare or blur fails. The measures are text sharpness, OCR confidence and glare. Categories may also carry an optional **retention rule** that suggests archiving, never automatically: for example, receipts older than 3 years unless tagged as a warranty, or parking-spot notes after a month.

   Because a bad photo can still be the only record of something, **value protects it**:
   - it is the only photo of its event or day;
   - it shows a named person who appears rarely in the library;
   - it is old (scans, pre-2010).

   Protected photos are shown with a warning ("only photo of this event") and are never put into a bulk approval. Floor rejects are always the `review` tier, the UI shows the worst first, and the false-archive rate for singles is measured separately on the sample.

- *v1:* single-elimination tournament at 512 px, one order only, "A wins" on error, keep count fixed by group size, and never fewer than one frame per burst. Singles were never judged.

### S6 Review: a bundle at the root of the storage

The pipeline doesn't only run on OneDrive. It also runs on **external drives** plugged into Mimir and on **network shares**. Whoever reviews may be someone else, on their own computer. So the review tool isn't a server. It's a **review bundle**, a folder written at the root of each library that travels with the photos (user requirement, 2026-11-03; decisions.md P21).

**Libraries.** A library is a storage root (the OneDrive mirror, a drive mounted under `/mnt`, an SMB/NFS share), always mounted **read-only** for processing. Each has its own DB and cache on Mimir (`/srv/mimir/data/photos/<library>/`, `/srv/mimir/cache/photos/<library>/`) and an ID in `OrderMyPhotos/library.json`, so the drive is recognized when it comes back. Curated folders are processed **album by album**, so albums finish one after another instead of all at the end.

**Bundle layout** (inventory never treats it as photos):

```
<root>/OrderMyPhotos/
  index.html               every album: ready / processing (n of m) / reviewed / trusted / applied
  albums/<album>.html      one review page per album that's ready
  t/<id>.jpg               512 px thumbnails: the only copies of the photos (~45 KB each, ~8 GB for 175k)
  p/<id>.jpg               1536 px previews, only for formats a browser can't show (HEIC, RAW, MPO)
  decisions/<album>.json   what the reviewer decided and what was applied
  library.json, README.txt
```

The open view loads the **original file through its relative path** (`../../<album>/<file>`), so the bundle stays small and the reviewer sees full resolution. Browsers apply the EXIF orientation themselves. Nothing is loaded from the internet. The bundle holds thumbnails, captions and names derived from the photos, and it sits on the same storage as the photos, so it reaches nobody new. Sensitive numbers are never in it.

**Reviewing.** Per album, the reviewer either labels it (keep / archive / bad, fix bursts, categories) or **trusts the automatic picks**, which accepts every proposal of that album as it stands. Trusting works from the index too, album by album. Whatever is decided is saved in `decisions/<album>.json`. The next run on Mimir reads those files back as labels, which train the thresholds and the ranker.

**Applying on an external drive or share** happens on the reviewer's computer, in the page. Chrome and Edge allow it through the File System Access API; in Firefox and Safari the page can review and export but not apply.
1. The reviewer picks the storage root, and the page checks `library.json`.
2. Before each photo is archived, the page checks it is still the file that was reviewed (size + QuickXorHash, computed in the browser).
3. The page moves it to `<root>/Archive/OrderMyPhotos/<original path>` and logs the op in the album's decisions file.

**Deleting** is the last step, also in the page (user requirement, 2026-11-03). Once an album is applied, the page offers to delete its reviewed archived photos, and does it only after the reviewer confirms. The confirmation shows the number of files, their total size and the list, with thumbnails. Only photos that went through review or an explicit trust are eligible, and each is hash-checked again before it's deleted. Until then they sit in `Archive/OrderMyPhotos/`, where a restore is one move back from the log. The decisions file records what was deleted and when.

**Applying on OneDrive** stays on Mimir (S9). The mirror is read-only, so Mimir **uploads the bundle into OneDrive** (`OrderMyPhotos/` at the drive root, through `mimir-onedrive`), album by album **as each album is ready**, while the rest is still processing. The reviewer opens it from their synced OneDrive folder; Files On-Demand downloads an original when it's viewed. The page writes the decisions into `OrderMyPhotos/decisions/`, OneDrive syncs them up, and Mimir picks them up on the next pull. It applies them with `mimir-onedrive apply`: server-side moves, hash-checked and logged. The reviewer's PC doesn't move the files itself; that would also work through the sync client, but it would bypass the hash checks and the op log. Deleting works the same way: the reviewer confirms in the page, the page writes a delete request into the decisions file, and Mimir deletes those files from the archive, after which OneDrive's recycle bin keeps them another 30 days.

**An album is ready** when all its files are in the library (for OneDrive: synced, with nothing pending for it), and every stage up to captions is done for its photos. Then its page and thumbnails are written, and uploaded for OneDrive. The index shows the other albums as processing.

**Later, a server view:** the FastAPI + HTMX app on Mimir (`127.0.0.1`, Tailscale Serve) for what doesn't fit in a static page: the Tags and Albums views, and the daily digest of live mode (S10). It reads and writes the same decisions files.

The page itself:
- The album view shows each burst as a filmstrip with the picks highlighted and badges: blur, eyes closed, below the floor, v1 pick, confidence. Below the bursts comes a **"bad on their own"** strip: floor rejects with their reasons, and a warning on value-protected ones ("only photo of this event").
- Keyboard-driven, with the left hand on the home row and the right on the arrows: `a` archive, `s` keep, `d` bad on its own, `g` / `x` join or leave the previous photo's burst, `o` takes a photo out of the album or proposal without archiving it. S11 then routes it to the thematic folder it fits, never back to where it came from. ←/→ move, `Enter` accepts the group, `Space` opens the photo. The open view shows the photo at full resolution beside the thumbnails of its burst (or a single's neighbours), its reasons and its caption. If the original isn't reachable (a copy of the bundle without the photos), it shows the thumbnail and says so.
- Readable in light and dark themes: text on the green, amber, blue and red fills is white in the light theme and dark in the dark one, and every text element is at least 4.5:1 against its background (measured on the sample bundle).
- Approving an album, by labelling or trusting it, marks its decisions ready for the plan.
- Every override is stored as a label, and the labels train the S5 ranker.
- The same app has a **Tags** view (vocabulary candidates), an **Albums** view (event album proposals, plus reference photos routed to category albums, with the probe's and the VLM's verdicts) and the **daily digest** for live mode (S10).
- **Immich** ([decisions.md](decisions.md) P6) is the browsing, search and **people** UI. It runs as a read-only external library on the *final* layout, after the culling moves. Immich treats a moved file as a new asset, so pointing it at the library before then would waste work. Our own UI is only for what Immich has no workflow for: culling, album proposals and the digest.

### S7 Captions and tags (VLM, kept photos only)

- **One request per photo** with a JSON schema enforced by llama-server's `response_format`. The fields: `title`, `description` (1–3 sentences), `tags[]`, `scene`, `activities[]`, `objects[]`, `text_in_image`, `kind`, `people_count`, and `subjects[]`: each person and animal with what they're doing ("a skier climbing", "a dog catching a ball"), so the description says it and the action becomes a tag (P22). This replaces v1's three chat turns.
- **Context block,** computed and not rolling:
  - the album folder name (which often holds the place and people: "`<place> z <name>`");
  - the event summary;
  - the place from **offline** reverse geocoding: town or village, gmina, region, country, in Polish. This uses a self-hosted **Photon** with an OpenStreetMap Europe dump in reverse-only mode, with GeoNames `cities1000` as the fallback outside Europe;
  - season, part of day and holidays, derived from local time (never the raw timestamp);
  - the named people in the frame with their positions;
  - the user's `context.txt`.
  The prompt says to use the context only where the image agrees with it.
- **Two passes, so photos can run in parallel:**
  1. Captions with static context, on several llama.cpp slots in parallel.
  2. A text-only LLM pass that summarizes each album or event from its captions.

  An optional third pass, also text-only, harmonizes titles inside an album.
- **Tags:**
  - A curated, hierarchical vocabulary (`vocab.yaml`, ~300–600 entries such as `Places/Mountains`, `Activities/Ski touring`, `Events/Birthday`), with English and Polish labels and synonyms.
  - The VLM's free tags are mapped to the vocabulary by Qwen3-Embedding nearest neighbour.
  - Tags that don't match go to a review queue for extending the vocabulary.
  - Each tag is checked against the image with SigLIP 2 similarity, using per-tag thresholds, to drop hallucinations.
  - Multi-word tags are allowed. Colors are tagged only when they dominate.
- **Reference photos get a different prompt:**
  - the title names the thing ("Tabliczka znamionowa pralki Bosch");
  - the description summarizes the content and repeats the **key codes** (model, serial, MAC, amounts, dates), so OneDrive and Windows search can find them;
  - `text_in_image` holds a short excerpt. The full transcription comes from the OCR stage below.

  Text in **Sensitive** photos (card numbers checked by Luhn, PESEL by its checksum, IBAN, ID numbers) stays in the DB. It is never written into file metadata or captions.
- **Text pages** (`omp text`, decisions.md P24, P34): for photos that the zero-shot kinds or the captions call text-like (documents, book pages, notes, receipts, labels, screens), before grouping:
  1. **Is it a page of text?** The resident VLM is asked whether there's text, then whether the photo is mostly a page of text (about 1 s each). A page gets the **"OCR needed"** tag until the OCR has read it.
  2. **Orientation.** The resident VLM is asked "is the text upright?", about the photo as it is and, if not, about each rotation, and the best rotation wins. Asked which way a page is turned, it got the direction wrong. A turned page gets a **lossless rotate** proposal (EXIF Orientation, no re-encode), and the OCR reads it upright.
  3. **OCR, a post-process** (`omp ocr`, P34): once the albums are done and the photos sorted, Unlimited-OCR-2 (mimir `stacks/ocr`, on the 4090 through llama-swap, evicting the chat model) reads the kept pages at up to 2560 px. It keeps the layout blocks (P28), then the guarded repair runs. Tables (receipts, price lists) become Markdown tables.
  4. **Saved with the photo.** The text goes into the DB with an FTS5 index. A photo that is **mostly text** gets a **`.txt` file of the same name next to it** when the plan is applied (the bundle keeps a copy under `OrderMyPhotos/text/`).
  5. **Its own subfolder.** In an album, the text pages move to the album's **`Dokumenty/`** subfolder (configurable), so the album shows the trip and the pages stay next to it (user, 2026-11-03).

  Different pages shot together form a **document set** (S4): they're never culled against each other, the review page shows "page 2/4", and a set can later become one searchable PDF.
- **Search:**
  - description and tags are embedded with Qwen3-Embedding-4B, and the image with SigLIP 2;
  - `text_in_image` and the captions also go into a **SQLite FTS5** full-text index, because embeddings are bad at exact strings like `SN: 2231A4F07`.

  All of it is in the DB for the UI search.
- *v1:* three turns, thinking on by accident, raw timestamps in the prompt, online geocoding, a rolling summary that drifted, 10 single-word tags with no vocabulary.

### S8 People (faces)

- Faces come from S3, for every photo including archived ones, because more samples make better clusters. They use the same InsightFace `buffalo_l` models that Immich uses.
- Faces that are tiny, low-confidence or low-quality are left out of clustering.
- **Clustering** runs library-wide on the identity embeddings (HDBSCAN or Chinese Whispers, whichever does better on the sample). A person can own several clusters (children change with age), and unassigned faces stay unassigned.
- **Naming happens in Immich** (P6), which already has merge, hide, rename and a review flow:
  - Our clusters are pushed as people and faces through the API (`POST /api/people`, `POST /api/faces`), or Immich clusters on its own.
  - The names are read back (`GET /api/people`, `GET /api/faces`) and matched to our faces by box overlap.
  - If Immich turns out not to fit, the fallback is a People view in our own UI.
- **Propagation:** each new face goes to the nearest named centroid above a threshold. Below it, the face goes to a review queue. In live mode, faces of known people are named automatically.
- **Output:**
  - MWG `RegionInfo` face regions (Type Face, normalized area, Name), which Immich and digiKam read; optionally `XMP-MP:RegionInfoMP` for Microsoft tools;
  - `People/<Name>` keywords, so OneDrive and Windows search find them;
  - names in the caption context.
- *v1:* none.

### S9 Apply to OneDrive

- Every change is a **plan**, a JSONL list of ops: `move(item_id, from, to)`, `put_meta(item_id, file built in the work area)`, `restore(…)`. Each op carries the hash it expects to find on OneDrive.
- `mimir-onedrive apply <plan>` runs as `onedrive`:
  1. Takes a snapper `onedrive` snapshot.
  2. Executes the plan with rclone server-side moves (`moveto` keeps item ID, sharing and version history) and content uploads for metadata.
  3. Skips any op whose expected hash no longer matches (the photo changed on OneDrive in the meantime).
  4. Logs every op.

  The next pull brings the result back into the mirror.
- **Archive layout:** a single root outside the albums, for example `Archive/OrderMyPhotos/<original path>`. Restoring is a move back, and albums stay clean (v1 put `_archived/` inside every album). Deleting from the archive is a separate, explicit plan, and OneDrive's recycle bin adds 30 more days.
- **Metadata goes in once per file.** Titles, descriptions, tags, people and rating are written together with exiftool 13.x after captions and names are final. Only the metadata segments change; pixels are not re-encoded. Each file is then re-uploaded once to the same item (`PUT /items/{id}/content` keeps the ID and version history): ~0.5 TB for all photos kept, network-bound, spread over days. Sidecars don't work here, because OneDrive and Windows never read them (P5). Fields written, so that Windows, OneDrive, Immich and digiKam all agree:

  | What | Fields |
  |---|---|
  | Title | `XMP-dc:Title`, `EXIF:XPTitle` (Windows reads XPTitle first) |
  | Description | `XMP-dc:Description`, `EXIF:ImageDescription` (Immich reads this first; camera junk like "OLYMPUS DIGITAL CAMERA" is cleared), `IPTC:Caption-Abstract` |
  | Tags | `XMP-dc:Subject` + `IPTC:Keywords` (Windows, OneDrive), `XMP-lr:HierarchicalSubject` + `XMP-digiKam:TagsList` (Immich reads these, not dc:subject), `IPTC:CodedCharacterSet=UTF8` for Polish characters. Never `XPKeywords` |
  | People | MWG `RegionInfo` + `People/<Name>` keywords |
  | Rating | `XMP-xmp:Rating`, from the S5 score for kept shots (optional) |
  | Provenance | `XMP-xmp:CreatorTool` + the `omp` namespace (version, run id) |

  OneDrive Photos keeps its own tags in the service, and there is no official word on whether its web search indexes embedded keywords. This is measured on the sample album before the big upload. The OneDrive item `description` (Graph `PATCH /items/{id}`, no re-upload) is a cheap extra channel for the description. Videos are not re-uploaded just for metadata (S12).
- **v1 reconciliation:** after review, a frame v1 archived that v2 keeps moves back into its album, a frame v2 culls moves to the new archive, and the `_archived/` folders and `burst_*.json` files are removed. Their content is kept in the DB.

### S10 Live mode (new phone photos)

The phones upload to OneDrive: Samsung Gallery sync and OneDrive camera upload. The hourly pull brings the photos to the mirror. `omp-live.timer` then runs `omp live`, which takes everything new or changed since its last run through the same stages:

1. **S0–S3:** inventory, previews and signals. Live batches are small (tens to hundreds of photos a day), so the vision signals run on the **B50**, which has ~8 GiB free next to the RAG models. Captions use the **resident** Qwen3.8, with no swap. A live run never evicts the chat model.
2. **S1/S4 copies against the whole library:** a photo that already exists elsewhere (copied into an album by hand, or sent back through WhatsApp) becomes a copy group straight away.
3. **S4/S5 bursts:** decided once nothing new has arrived for a while (e.g. 2 h), so a burst isn't judged half-uploaded.
4. **S7 captions + S8 faces:** new faces are matched to known people, and unknown faces wait for naming.
5. **S11 albums:** memories join an open event. An event closes after ~12 h without photos, or when the device is back home after a trip. A closed event becomes an album suggestion, or is added to an existing album. **Reference photos go straight to their category album** ("3 receipts → Paragony, 1 label → Hardware"), and their text is searchable within the hour. A photo of a serial number taken today can be found by its code tonight.

**Review:** the web UI collects everything a live run proposes into a **daily digest**, for example "143 new photos · 12 bursts → archive 31 · 4 copies · new album '2026-11-07 …' (86 photos)". The user approves it in a minute. Some op classes can be marked auto-apply once the numbers have proven safe; exact copies and metadata writes are the likely first. Until then, nothing touches OneDrive without approval. A phone notification is optional (it ties in with mimir Phase 7 alerts).

**Risk to check first:** how phone-side sync reacts when the cloud copy is moved or deleted. OneDrive camera upload only uploads, but Samsung Gallery's OneDrive sync may mirror cloud deletes back to the phone. The apply step must not empty a phone's gallery. Until this is tested, photos that arrived through a two-way sync folder are copied into albums, not moved (decisions.md P8).

### S11 Albums for unsorted photos

There are two kinds: **event albums** for memories, described here, and **category albums** for reference and media photos (S3b), which take their folder from `categories.yaml` and their placement rule from the table there. Reference photos are taken out *before* event segmentation, so a run of receipts at home doesn't create a fake event and a label shot mid-trip doesn't split the trip. A category album with many photos gets year subfolders (`Pictures/Info/Paragony/2024/`).

- **Input:** photos and videos whose folder policy is `unsorted`: `Obrazy/Samsung Gallery`, `Camera Roll`, `Obrazy/Other`, `Pictures/Photos`, WhatsApp, Quick Share, …. Copies are resolved first, so a phone photo the user already copied into an album counts as "in an album".
- **Events:**
  1. Segment one time stream across all devices. A boundary falls where the gap is long compared with the local shooting rate (for example, more than max(2 h, k × the median gap nearby)), or where the location jumps.
  2. Merge consecutive days away from **home** into one **trip** event, with one sub-event per day. Home is the most frequent night-time GPS cluster for that period.
- **Existing album first:** an event that overlaps an existing album's date range becomes "add these 23 photos to *`<YYYY-MM-DD> <trip name>`*". These are phone shots that were never copied over.
- **New album otherwise:**
  - It's named in the existing style, `YYYY-MM-DD <name>` (trips may use `YYYY-MM <name>`).
  - A text-only LLM call writes the name in Polish from the places, named people, holidays and the event's captions. A few dozen of the user's 632 album names serve as style examples.
  - Events below a minimum size (8 kept photos) don't get an album. Their photos go to a thematic folder (P29): a named person's folder, the user's thematic folder they look most like, `Other/Widoki`, `Other/Dom` or `Other/Random`. No photo stays unsorted.
- **Ground truth for free:** the 632 existing albums. Running the segmenter over their photos, mixed into one stream, shows how well it reproduces the user's album boundaries (boundary F1, adjusted Rand index) before it proposes anything new.
- **Review and apply, in the bundle (S6):** a folder of thousands of unsorted photos is never one review page. Each **proposed album** is its own unit in the bundle's index, next to the existing albums, marked as a proposal: "proposed: `<YYYY-MM-DD> <name>` · 86 photos · from `Obrazy/Samsung Gallery`". Its page is the same review page (bursts, keep / archive / bad, trust), plus:
  - the proposed name and date, editable;
  - "merge with the previous / next proposal" and "split here" at a photo;
  - "add to the existing album …" when it overlaps one;
  - "Not an album: thematic folders", which routes its photos like a small event's.

  Every photo ends up in an album or a thematic folder (P29).
- **Apply:** an approved proposal creates the album folder and moves its kept photos into `Pictures/Albums/<year>/<name>/`, while its culled photos go to the archive like any other. Reference photos go to their category albums the same way. On an external drive or share the page does this itself, like the other moves. On OneDrive `mimir-onedrive apply` does it, and photos from Samsung Gallery's synced folders are moved too, with the phone's copy cleaned (P30). Moves happen only in the production run, after review, never in a sample run. Immich albums can follow later, through its API.
- **Rules, in order** (decisions.md P25, P29–P32):
  1. Already in an album → archive the copy.
  2. Downloaded images (memes, wallpapers, art, infographics, screenshots) → a media folder, never an event album, even when they arrived during the trip (P31).
  3. Messenger photos (WhatsApp, Messenger, Facebook) carry only the day they arrived, so each is decided on its own. A real photo joins an album it arrived during (or up to 3 days after) if it looks like it, or one within 60 days it looks very much like, never on the day alone. A document goes to its category folder, and the rest to a thematic folder (P32).
  4. **Taken while an album's event or trip was going on → it joins that album, whatever folder it's in.** Text pages go into its `Dokumenty/`, and full nudity into its ` PRIV` sibling (P26).
  5. Other reference photos → their category album.
  6. Other events → a new album, or a thematic folder when too small (P29).

  Steps 1–4 run as their own stage, `place`, before grouping (S4). A photo that joins an album is grouped, ranked, captioned and reviewed with that album: it can form a new burst with the album's photos, and it shows on the album's page (P33).

  Album folders outside `Pictures/Albums/<year>/` are proposed to move there (P30). The library's albums come from the OneDrive manifest, so the rules see the whole library.
- **Progressive:** a proposal is ready when its event is closed (no photos for a while, or the next event has started) and its photos are processed. The folder is processed from the oldest photo onwards, so review can start long before a folder of 43k photos is done.

### S12 Videos

Videos are 4,786 files and 359 GiB, 37% of the library, and very lopsided: 56 files over 1 GB hold 110 GiB, and 340 files over 200 MB hold 226 GiB. They go through the same stages wherever it makes sense:

| Stage | For videos |
|---|---|
| S0 inventory | `ffprobe`: duration, codec, bitrate, resolution, fps, rotation, creation time, QuickTime GPS |
| S1 exact copies | **297 redundant copies, 46.8 GiB**: the same rule as for photos |
| S4 copies | Re-encoded, trimmed or WhatsApp (`VID-…-WA`, 277 files) versions. Detected by keyframe DINO embeddings aligned in time, plus a Chromaprint audio fingerprint. The highest-quality original is kept |
| Junk clips | The same quality floor as for photos (S5 step 8). 1,976 videos are under 10 MB. Very short, dark, pocket, extremely shaky or empty clips become archive suggestions in the review UI, never automatic, with the same value protection |
| S3 signals | Scene-change keyframes (capped per minute), decoded with NVDEC, with Q-ReAlign, faces and SigLIP on each keyframe. Audio: loudness, speech detection, and a **transcript** from a Polish-capable STT on the B50 |
| S7 captions | The same VLM call with 4–8 keyframes plus a transcript excerpt gives the title, description and tags. The transcript is also searchable |
| S8 people | Faces from keyframes |
| S11 albums | Videos join events exactly like photos |
| S9 metadata | **Not re-uploaded just for metadata** (359 GiB). The description goes to the OneDrive item `description` through Graph, and tags and people go to the DB and Immich. XMP goes into the MP4 only when the file is rewritten anyway (re-encode below) |

**Re-encoding, optional and lossy** (P9, opt-in per run):
- Candidates are old high-bitrate files: MJPEG AVI from compacts (`MVI_…`), AVCHD `.mts`, early 4K H.264 from phones, and anything over ~200 MB.
- The target is AV1 on the 4090's NVENC (or the B50's AV1 encoder), gated per file by **VMAF** (e.g. ≥ 95), with creation time, GPS and rotation carried over (`ffmpeg -map_metadata 0` + exiftool).
- The original goes to the archive and is deleted only after review.
- Savings and OneDrive/phone playback of AV1 are measured on the sample before anything runs at scale. HEVC is the fallback codec.

**Samsung Motion Photos** are JPEGs with a short embedded MP4. Measuring how much they add is part of S0. Stripping the motion part from kept photos is a later, opt-in option (it changes the files but not the still image).

### Space report

The UI shows where the space goes and what each action would free:
- exact copies (68.8 GiB: 22.0 photos + 46.8 videos);
- copies across folders;
- culled bursts (v1's archive held 117 GiB, for reference);
- bad singles below the quality floor;
- junk clips;
- re-encode estimates;
- Motion Photo parts.

Every number is shown before anything is approved.

## Running it on Mimir

| Step | Resource | Rough cost (to be measured on the sample album) |
|---|---|---|
| S0–S2 hash, metadata, previews | CPU (20 procs) + NVMe | ~1–3 h for 615 GiB |
| S3 cheap signals | 4090 (Qwen3.8 evicted) + CPU | a few hours: embeddings run at hundreds of images/s; IQA and faces at tens to hundreds |
| S3 expensive signals (burst members and floor candidates) | B50 depth (~80 ms per image) + CPU sharpness | ~1–3 h for ~50–80k photos |
| S4–S5 grouping and scoring | CPU | minutes |
| S5 VLM judge (close calls) + "worth keeping?" (borderline singles) | 4090 Qwen3.8 | ~10–25 h (say 10–20k pair verdicts × 2 orders, plus a few thousand single checks) |
| S6 review | the user | album by album, over weeks; auto tiers can be bulk-approved |
| S7 captions (~150k kept) | 4090 Qwen3.8 batch variant (parallel slots) + optionally a smaller VLM on the B50 | ~2–3 days |
| S8 clustering | CPU | minutes; naming is the user |
| S11 events and album names | CPU + text-only LLM | minutes to an hour |
| S12 video probe, keyframes, fingerprints | NVDEC + CPU | hours (359 GiB to read) |
| S12 transcripts | B50 STT | depends on the total duration, which is not known yet (S0 measures it) |
| S12 re-encode (opt-in) | 4090 NVENC | hours per 100 GiB; the VMAF check doubles the decode |
| S9 upload | network | days for the metadata re-upload |
| S10 live mode | B50 + resident Qwen3.8 | minutes per day, no model swaps |

- **A batch variant of Qwen3.8** (`qwen3.8-27b-batch`: several parallel slots with small per-slot contexts) is added to llama-swap for S5/S7. It shares the weights, so swapping between it and the chat variant costs ~10 s. While it runs, Open WebUI chats are slower, or have to wait their turn. Long runs are planned for nights, or announced.
- **Paths on Mimir:** code at `/srv/mimir/stacks/photos/` (compose and `.env`), DB at `/srv/mimir/data/photos/`, cache at `/srv/mimir/cache/photos/`, plans and logs at `/srv/mimir/onedrive/plans/`. Each is set up and logged in the mimir repo like any other stack.

## Evaluation

Each change to grouping, ranking or captions is measured on the sample album before it touches the library:

- **Grouping:** pairwise precision and recall against the user's corrected bursts, reported separately for short gaps (< 1 min) and long ones (minutes), because the long ones are the hard part.
- **Picks:** top-1 agreement with the user, and the **false-archive rate**: shots the user would keep but v2 archives. This is the safety metric, with a target of ~0. v1's picks on the same album are the baseline.
- **Quality floor:** precision and recall of "bad on its own" against the user's marks on the sample, with the false-archive rate for singles reported on its own line.
- **Copies:** precision on a hand-checked sample of cross-folder pairs. A false "copy" archives a different photo, so precision is the metric that matters.
- **Albums:** boundary F1 and adjusted Rand index against the 632 existing albums, and how often the user renames or splits a proposal.
- **Kinds:** category accuracy on a held-out part of the already-sorted `Info/*` and `Other/*` folders, and above all how often a *memory* is wrongly called reference. That error would move it out of its event album, so the target is ~0.
- **Captions:** hallucinations per 50 photos, tag precision and recall on a hand-checked subset, and Polish/English quality, if both are used.
- **Video:** junk-clip precision; the copy detector on WhatsApp pairs; VMAF and size for the re-encode sample.
- **Speed:** images/s per stage, so the full-library cost in the table above is measured, not guessed.
