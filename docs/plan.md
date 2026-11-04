# Plan

Phased so that each step is measured on the sample album before it touches the library, and the library is changed in waves, each one reviewed. Tick the boxes as work lands. Changes to Mimir itself (stacks, models, llama-swap entries, subvolumes) are also logged in the mimir repo's `docs/changelog.md`.

## Phase 0 — Restart

- [x] Review v0/v1 and record what they did and left in OneDrive → [legacy.md](legacy.md)
- [x] Archive the old code (`archive/`, tag `legacy-final`)
- [x] Research current models (2026-11-02) → [models.md](models.md)
- [x] Design, decisions, this plan
- [x] Photos purged from the public repo's history; `master` and `delete_logs` force-pushed (decisions.md P15)
- [ ] **You:** optionally ask GitHub Support to purge the old commits' cached views (P15), and close the now-empty PR #1
- [ ] **You:** answer the other open questions in decisions.md (language, archive, edits, folders); defaults are used until then

## Phase 1 — Groundwork on the sample album

- [x] **You:** pick the sample album: a 2022 ski-touring album, 71 photos (50 + 21 v1-archived), v1-culled and v1-captioned, no videos and no people close up. Results: [sample-runs.md](sample-runs.md) run s1
- [x] `omp` package skeleton: `pyproject.toml`, `policy.yaml`, `vocab.yaml`, SQLite schema (WAL), a stage CLI where each stage skips what's done for the same model, a pytest for the hash. Still to do: schema migrations, a uv lock, ruff in CI
- [x] Image `mimir/omp:0.1`: one image for the CPU stages and PyTorch XPU (B50), built from `docker/Dockerfile`, dependencies pinned in `docker/requirements.txt`
- [ ] Image for the 4090 (PyTorch CUDA 13 + onnxruntime-gpu), served through llama-swap for bulk runs. Video tools (ffmpeg, Chromaprint) and rawpy come with the video and RAW stages
- [x] Mimir: `/srv/mimir/data/photos`, a `/srv/mimir/cache` subvolume (no snapshots) with `photos/`, and `stacks/photos/` (compose + `sample.env`) in the mimir repo
- [x] S0–S2 on the sample: inventory, QuickXorHash (matches OneDrive), capture times and their sources, v1 traces, previews and the full-resolution sharpness grid. 5.9 photos/s on 20 cores, too slow for the library (~8 h): to tune
- [x] A minimal review page to **label the sample**: a static `index.html` per run with thumbnails and previews (`omp report`). It fixes bursts, verdicts (keep / archive / bad on its own) and reference categories, with keyboard shortcuts. Labels are exported as JSON and imported with `omp labels`
- [x] **You:** label the sample on that page and export the labels (70/71 on 2026-11-03; `omp evaluate` measures a run against them)
- [x] **You:** label the four more albums of run s2 (2009 compact, 2015 wedding, 2021 two-camera trip, 2022 family), so the rules are fitted on more than one album

## Phase 2 — Grouping and picking (sample)

Run s1 (2026-11-03) did a first pass of every stage below on the B50 and the resident Qwen3.8, with hand-set thresholds. The boxes are ticked once a stage is calibrated against the labels.

- [ ] Cheap signals: DINOv2-B vs DINOv3-L, SigLIP 2, pHash, Q-ReAlign Mini, `buffalo_l` faces, sharpness/exposure/noise. Measure img/s on the 4090 and the B50
- [ ] S3b kinds: `categories.yaml`; a SigLIP 2 linear probe trained on `Pictures/Info/*`, `Pictures/Other/*`, the invoice/scan folders, `Screenshots` and album photos (these are already on Mimir once the sync finishes, no sample needed); planarity and text-density features; measure accuracy and the memory→reference error
- [ ] S4 groups (copies, bursts): calibrate the similarity-over-time curve, the GPS distance, the viewpoint check and the cluster diameter against the labels; report precision and recall for short and long gaps; compare with v1's groups
- [x] Subjects and actions (P22), first pass: stored, but they don't separate the user's choices (P23), so they're not in the keep decision yet. Retry with pose sequences. Original item: RT-DETR people and animal boxes, ViTPose++ poses, facing and cut-off, SigLIP 2 zero-shot action on each crop. Use them in the keep count ("a main subject does something else"), in a "moment" score, in the judge (subject crops) and in captions (`subjects[]`). Measure on the s1 + s2 labels: false archives prevented against false keeps added, and img/s on the B50
- [ ] Expensive signals on group members: depth (V2-Small vs DA3), BiRefNet, subject-masked sharpness, MediaPipe blink/smile, EmotiEffLib, eDifFIQA
- [ ] S5 ranking: hard rejects, feature score, MMR keep count; measure top-1 agreement and the **false-archive rate** against the labels and against v1
- [ ] Quality floor (P14): the pre-screen, the absolute thresholds per camera/era, the VLM "worth keeping?" verdict and value protection; measure precision and the singles' false-archive rate
- [ ] VLM judge: measure Qwen3.8 vision latency at ~1 MP, the both-orders + logprobs protocol, and Bradley–Terry; check whether it beats features alone on near-ties
- [ ] Optional: Phototriage (if a mirror of the dataset can be found) for tuning the feature weights
- [ ] Review UI: filmstrips, badges, overrides stored as labels, refitting the ranker from them

## Phase 3 — Captions, tags, places (sample)

- [ ] Photon on Mimir (Europe dump, reverse-only, `pl,en`), plus the GeoNames fallback; a mimir stack
- [ ] Context block (album name, place, season and part of day, holidays, people) and a JSON-schema prompt, one call per photo
- [ ] `vocab.yaml` v0 (hierarchical, EN + PL), SigLIP 2 zero-shot with per-tag thresholds, VLM tags mapped with Qwen3-Embedding
- [ ] VLM bake-off: Qwen3.8-27B vs Qwen3.6-35B-A3B (IQ4_XS) vs Gemma 4 26B-A4B. Measure s/image, hallucinations per 50 photos, and Polish quality in a blind test
- [ ] `qwen3.8-27b-batch` llama-swap variant (parallel slots, small per-slot context): measure throughput against chat latency
- [ ] Album/event summary pass (text-only) and title harmonizing
- [ ] Reference prompt: full transcription, key codes in the description, Sensitive detection (Luhn, PESEL, IBAN) kept out of metadata; SQLite FTS5 search over `text_in_image`; legibility measures for the quality floor; document sets

## Phase 4 — People (sample, then a larger slice)

- [ ] Clustering: HDBSCAN vs Chinese Whispers on `buffalo_l` embeddings, with quality filters; check purity by hand
- [ ] Immich stack on Mimir: read-only external library over a test folder; ML on CUDA vs OpenVINO on the B50
- [ ] Push clusters to Immich, name them there, read the names back, match faces by box overlap
- [ ] Write MWG regions + `People/` keywords; check that Immich, digiKam and Windows show them

## Phase 5 — Write-back trial on the real OneDrive (sample)

- [ ] `mimir-onedrive apply <plan>`: snapshot, hash-checked ops, server-side `moveto`, `PUT content` to the same item, an op log, dry run
- [ ] What shows up where: embedded title, description and keywords in OneDrive web/mobile search, Windows Explorer and Immich; the Graph item `description`
- [ ] **Two-way sync test** (P8): move and delete a throwaway photo in the cloud copy of the Samsung Gallery folder, and watch the phone
- [ ] The archive layout, plus restore as one move back

## Phase 5b — Review bundle and other libraries (design.md S6, decisions.md P21)

- [x] `omp report` writes the bundle at the library root: `OrderMyPhotos/index.html` with every album ready or still processing, one page per ready album, 512 px thumbnails, originals shown through relative paths, "Trust automatic picks" per album, decisions exported per album or all at once (`omp labels` imports both)
- [ ] Apply in the page (File System Access API, Chrome/Edge): pick the storage root, check `library.json`, hash-check each file, move it to `Archive/OrderMyPhotos/<path>`, log it in `decisions/<album>.json`, restore from the log. Tested on a scratch copy first
- [ ] Delete in the page: after apply, delete the reviewed archived photos once the reviewer confirms, seeing the count, size and list; hash-checked again; logged
- [ ] The page saves decisions straight into `decisions/` (File System Access API) instead of a download; `omp` reads that folder back as labels on the next run
- [ ] Libraries: a `library.json` ID, a DB and cache per library on Mimir, external drives and shares mounted read-only (mimir side), album-by-album processing order, per-album readiness
- [ ] OneDrive: `mimir-onedrive` uploads the bundle album by album as albums are ready; the pull brings decisions back; `mimir-onedrive apply` executes moves and confirmed deletes
- [x] Text pages (P24): `omp text` (orientation from the VLM, Unlimited-OCR-2 on the 4090), FTS5, `.txt` per text page in the bundle, `Dokumenty/` placement, document sets by word overlap; the page shows the OCR text, "rotate", "page n/m" and "→ Dokumenty/"
- [ ] Apply for text pages: write the `.txt` next to the photo, set EXIF Orientation for the rotate proposals, move to `Dokumenty/`
- [ ] Decisions per file for photos stored in more than one curated folder: each folder is judged on its own (s2: the user kept the `Places` copy of an album photo that isn't the best of its burst)
- [x] OCR speed: base mode (one global view) tried in s4b on 3 pages. It's no faster than tiles (28–81 s per page) and worse on 5504 px camera photos, so tiles stay. Flattening the page (OpenCV outline + warp) found no outline on any of the 3. Qwen3.8 reading the photo directly is 3–5× faster with better diacritics, but it invents words, so it isn't used as the OCR (sample-runs.md s4)
- [ ] S11 first pass (run s4, P25): 483 unsorted photos from June–August 2019 with 2 more albums; library index from the OneDrive manifest; copy / taken-during / category / new album; LLM album names; proposal review units in the bundle (name, accept, "Not an album: thematic folders", `o` keeps a photo out). Full nudity to ` PRIV` albums (P26)
- [ ] S11 second pass (run s4c, P29–P32): no photo left unsorted (person, thematic, Widoki / Dom / Random folders); album folders outside `Albums/<year>/` relocated; downloads to media folders; 250 WhatsApp images decided one by one; people named (S8) for album names; the namer measured against the user's names
- [ ] S11 third pass (run s4d, P35): sessions and events as units, joins with place/people/look, LLM decisions with album and folder profiles, pet albums by identity, `Cats`, new-folder proposals; review pages per target with incoming marks, `m` to move. Measured against the s4 labels (outs, notes)
- [ ] Orientation for all photos (P36): the `orient` stage, its held-out accuracy and the turns it proposes, checked in review (`r`)
- [ ] OCR post-process (P34): `omp ocr` over the kept text pages once the sample's albums are reviewed; then retakes of one page found by their text
- [ ] Messenger thresholds (P32) checked on the first OneDrive batch, with every album embedded: arrival lag, look-alike 0.80 / 0.88
- [ ] Unsorted folders in the bundle (S11): one review unit per proposed album, not one page per folder. Editable name and date, merge / split / add to an existing album / leave in place; apply creates the album folder and moves the kept photos. First on a slice of `Obrazy/Samsung Gallery` (a few weeks, a few hundred photos), measured against the existing albums of the same weeks

## Phase 5c — The web app and the whole-library run (decisions.md P42–P44)

- [x] Folder policy for the run: `leave` for `Pictures/Private` and `Code`; wallpapers and the thermal camera ignored (P42)
- [x] Restart-safe stages: commits every 500 photos or 10 s, a clean stop after the photo in hand, bad photos skipped and logged, llama-swap calls retried, one lock per DB (P43)
- [x] Face clustering per connected component, so a whole library's faces fit in memory (P43)
- [x] `omp serve`: start and pause, the pipeline as a supervised child that resumes after a crash or reboot, progress, steps, skipped photos, timings, machine resources, results, a label API (P44)
- [x] Runs from a clone of this repo on Mimir (`compose.yaml`, `deploy/`); tested on a copy of s6: redeploy, pause, `kill -9`
- [x] The review in the app (P47): a wizard over sessions (a root folder each, one at a time), phases that stop for the reviewer, every album and folder one review unit (keep/archive, out, move, rename, dismantle, confirm), a resort loop that leaves confirmed photos alone, the People page saving to the app, the final decision
- [ ] The People page rebuilt in the app's style (it's the bundle's page, saving to the app)
- [ ] Apply the plan from the final decision: moves and the archive on OneDrive (`mimir-onedrive apply`, S9), hardlinks for a test set
- [x] When `fullrun` and the P45 sorting work are both on master (whoever merges second): albums.py's `vlm = VLM(cfg.llm)` becomes `VLM(cfg.llm, repeatable=True)` and the `:instruct` workaround wrapper under it goes (P46); the `OMP_PROMPT_LOG` wrapper stays
- [ ] S11 answers kept across runs (by prompt hash and model), so a re-run of `albums` doesn't redo ~25k LLM calls
- [ ] The neighbour table (top-20 look-alikes) at full scale: chunked on the GPU or with numpy
- [ ] **Later, speed-ups (user, 2026-11-04):** use every resource the machine has. When the 4090 is idle and will stay so for a while, it does the same work as the B50 too, from the same queue of photos (a CUDA worker, the `omp-vision-cuda` image and a llama-swap entry, so chat can still take the GPU back). Not the other way round: the 4090's work needs its VRAM and CUDA
- [ ] Optional: take the hashes from the OneDrive manifest (the same QuickXorHash, already checked by `mimir-onedrive verify`) instead of reading the whole terabyte again

## Phase 6 — Videos (sample)

- [ ] ffprobe, keyframes (NVDEC), Chromaprint, copy detection on WhatsApp pairs, junk-clip rules
- [ ] Speech-to-text on the B50, chosen with mimir Phase 4 (Polish is a requirement); transcripts in captions and search
- [ ] Re-encode trial (P9): AV1 NVENC vs HEVC, VMAF, size, playback on OneDrive web and the phone
- [ ] Motion Photos: measure how much the embedded clips weigh

## Phase 7 — Whole library, read-only

- [x] Wait for the first full OneDrive sync, `mimir-onedrive verify`, and the "pristine" snapshot (2026-11-04: 235,409 files, snapshot #38, read through `/srv/mimir/onedrive/pristine`)
- [x] The library instance of the web app on `:8445`, idle until the user starts pass 1
- [ ] S0–S4 over everything (photos and videos), then S3's expensive signals for group members
- [ ] Space report: exact copies, cross-folder copies, culling, junk clips, re-encode, Motion Photos
- [ ] Album segmentation measured against the 632 existing albums (boundary F1, adjusted Rand index)

## Phase 8 — Whole library, applied in waves

Each wave is planned, reviewed and applied, then checked after the next pull.

- [ ] Wave 1: exact copies (10,628 files, 68.8 GiB)
- [ ] Wave 2: cross-folder copies (resized, WhatsApp, edits per policy)
- [ ] Wave 3: culling, album by album: burst picks, bad singles below the floor, and v1's `_archived/` folders (restore or re-archive, then remove the folder)
- [ ] Wave 4: albums for unsorted photos: reference photos to category albums first, then memories to event albums, the rest to thematic folders; misplaced album folders moved; Samsung Gallery moved too and the phone's copy cleaned (P30)
- [ ] Wave 5: captions, tags and people, with one metadata write and one upload per file
- [ ] Wave 6: videos (copies, junk clips, opt-in re-encode)
- [ ] Wave 7: delete from the archive, as an explicit plan after a cooling-off period
- [ ] Point Immich at the final layout

## Phase 9 — Live mode

It can start as soon as Phase 5 works, running on new photos only.

- [ ] `omp-live.timer` after `mimir-onedrive-pull`: B50-only vision signals, captions on the resident Qwen3.8, no swaps
- [ ] Open events and bursts that are decided only once closed; new photos checked for copies against the whole library
- [ ] Daily digest in the review UI; auto-apply turned on one op class at a time, once it has proven safe
- [ ] Phone notification (with mimir Phase 7 alerts)
