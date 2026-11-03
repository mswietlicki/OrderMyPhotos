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
- [ ] **You:** label the sample on that page and export the labels

## Phase 2 — Grouping and picking (sample)

Run s1 (2026-11-03) did a first pass of every stage below on the B50 and the resident Qwen3.8, with hand-set thresholds. The boxes are ticked once a stage is calibrated against the labels.

- [ ] Cheap signals: DINOv2-B vs DINOv3-L, SigLIP 2, pHash, Q-ReAlign Mini, `buffalo_l` faces, sharpness/exposure/noise. Measure img/s on the 4090 and the B50
- [ ] S3b kinds: `categories.yaml`; a SigLIP 2 linear probe trained on `Pictures/Info/*`, `Pictures/Other/*`, the invoice/scan folders, `Screenshots` and album photos (these are already on Mimir once the sync finishes, no sample needed); planarity and text-density features; measure accuracy and the memory→reference error
- [ ] S4 groups (copies, bursts): calibrate the similarity-over-time curve, the GPS distance, the viewpoint check and the cluster diameter against the labels; report precision and recall for short and long gaps; compare with v1's groups
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

## Phase 6 — Videos (sample)

- [ ] ffprobe, keyframes (NVDEC), Chromaprint, copy detection on WhatsApp pairs, junk-clip rules
- [ ] Speech-to-text on the B50, chosen with mimir Phase 4 (Polish is a requirement); transcripts in captions and search
- [ ] Re-encode trial (P9): AV1 NVENC vs HEVC, VMAF, size, playback on OneDrive web and the phone
- [ ] Motion Photos: measure how much the embedded clips weigh

## Phase 7 — Whole library, read-only

- [ ] Wait for the first full OneDrive sync, `mimir-onedrive verify`, and the "pristine" snapshot
- [ ] S0–S4 over everything (photos and videos), then S3's expensive signals for group members
- [ ] Space report: exact copies, cross-folder copies, culling, junk clips, re-encode, Motion Photos
- [ ] Album segmentation measured against the 632 existing albums (boundary F1, adjusted Rand index)

## Phase 8 — Whole library, applied in waves

Each wave is planned, reviewed and applied, then checked after the next pull.

- [ ] Wave 1: exact copies (10,628 files, 68.8 GiB)
- [ ] Wave 2: cross-folder copies (resized, WhatsApp, edits per policy)
- [ ] Wave 3: culling, album by album: burst picks, bad singles below the floor, and v1's `_archived/` folders (restore or re-archive, then remove the folder)
- [ ] Wave 4: albums for unsorted photos: reference photos to category albums first, then memories to event albums (moves, or copies under P8)
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
