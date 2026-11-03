# Decision log

Short records of the choices that shape v2. New entries go at the bottom; supersede an entry rather than editing it. Mimir's own decisions (D1–D18) are in the [mimir repo](https://github.com/mswietlicki/mimir/blob/main/docs/decisions.md).

## P1 — Restart as v2; v1's results are suggestions only (2026-11-02)

v0 (C#) and v1 (PowerShell + Ollama) are frozen in `archive/` and under the tag `legacy-final`. What they did is recorded in [legacy.md](legacy.md). v2 starts from the design docs, in the same repository. **The user decided** that v2 redoes everything, including the 575 albums v1 already culled. v1's picks (`_archived/`, `burst_*.json`) and its captions are shown only as hints and used as a baseline, never as decisions.

## P2 — Python on Mimir, not PowerShell on the workstation (2026-11-02)

The models, both GPUs and the OneDrive mirror are all on Mimir, so the work goes where the data is. Python has the libraries (PyTorch CUDA/XPU, onnxruntime/OpenVINO, pillow-heif, rawpy, OpenCV, exiftool bindings). PowerShell's `System.Drawing` was Windows-only and ignored EXIF orientation. The code is one `omp` package with a CLI, run in containers like Mimir's other services.

## P3 — Local only; no external API in the pipeline (2026-11-02)

Hundreds of GB of private photos, GPS tracks and faces stay on Mimir. Every stage has a local model good enough for it ([models.md](models.md)). OpenRouter, or any external API, isn't part of the design. If one is ever wanted, it needs its own decision here and may only see text that contains no coordinates or names. Reverse geocoding is offline too (P12). v1 leaked GPS to Nominatim.

## P4 — Changes go through reviewed plans; archive first, delete later (2026-11-02)

The mirror stays read-only ([mimir D15](https://github.com/mswietlicki/mimir/blob/main/docs/decisions.md)). The pipeline produces a plan of hash-checked operations, the user approves it, and `mimir-onedrive apply` executes it as the `onedrive` user with server-side moves. Culled files move to a single archive root (`Archive/OrderMyPhotos/<original path>`), so albums stay clean and a restore is one move back. Deleting is a separate plan, and OneDrive's recycle bin adds 30 more days. v1 moved files inside the synced folder, with no review.

## P5 — Metadata embedded in the files, written once (2026-11-02)

OneDrive and Windows read only embedded metadata. Sidecars are seen only by Immich and digiKam. So titles, descriptions, tags, people and rating are written into the photo with exiftool (metadata segments only, no re-encode). Every field the different readers use is written ([design.md](design.md), S9). The write happens **once per file**, after captions and names are final, because each write means re-uploading the whole file (~0.5 TB in total). Videos are not re-uploaded just for metadata; they get the OneDrive item `description` and DB/Immich tags.

## P6 — Immich for browsing, search and people; own UI only for culling, albums and the digest (2026-11-02)

Immich (v3.3.x) already does what a photo UI needs:
- multilingual CLIP search (SigLIP 2 / NLLB-SigLIP, ~83% recall on Polish queries);
- face clustering and naming;
- stacks, albums;
- a full API.

It reads embedded tags (`TagsList` / `HierarchicalSubject` / IPTC) and MWG face regions. Immich has no culling workflow, so a small FastAPI + HTMX app covers the review of picks, album proposals and the live-mode digest.

Immich runs as a read-only external library on the **final** layout, because it treats a moved file as a new asset. Its ML runs on CUDA when the 4090 is free, or on OpenVINO on the B50, which needs testing because reports on Battlemage are mixed.

*Fallback:* a People view in our own UI, if Immich doesn't fit.

## P7 — GPU model servers under llama-swap; SQLite for state (2026-11-02)

The vision models are served by our own containers, which llama-swap starts on demand with measured `capacities`, like the depth service ([mimir D10](https://github.com/mswietlicki/mimir/blob/main/docs/decisions.md)). The capacity router evicts Qwen3.8 when a bulk vision pass needs the 4090 and brings it back afterwards, with no manual coordination. Live mode stays on the B50 and the resident Qwen3.8, so it never evicts chat.

State is one SQLite file (WAL). At ~200k photos, a few hundred thousand faces and a few embeddings each, it's 1–2 GB. Vector search is a brute-force matmul. Postgres or a vector database would add a service for no gain at this scale.

## P8 — Photos from two-way sync folders are copied, not moved, until tested (2026-11-02)

OneDrive camera upload only uploads, but Samsung Gallery's OneDrive sync may mirror cloud moves and deletes back to the phone. Until a throwaway photo has shown what happens, photos from such folders go into albums as copies, and nothing in them is archived. The test is in [plan.md](plan.md), Phase 5.

## P9 — Video re-encoding is opt-in, VMAF-gated and reversible (2026-11-02)

Re-encoding old high-bitrate video to AV1 (NVENC on the 4090) could free a large part of the 359 GiB, but it's lossy. It runs only on approved candidates. Each file must pass a VMAF threshold, metadata is carried over, and the original goes to the archive until the user deletes it. Savings and playback (OneDrive web, phone) are measured on the sample before anything runs at scale.

## P10 — Small events don't become albums (2026-11-02, default — to confirm)

An event with fewer than ~10 kept photos stays where it is, tagged with its event. One-off snapshots would otherwise turn into hundreds of tiny albums. The threshold is tuned against the existing 632 albums.

## P11 — Ranking: features first, VLM for close calls, both orders, Bradley–Terry (2026-11-02)

Cheap, explainable features decide most bursts: subject-masked sharpness, eyes/expression, Q-ReAlign, exposure. The VLM judges only the near-ties. Each pair is asked in both orders, the verdict is read from token logprobs, and the results are aggregated with Bradley–Terry. Position bias and asymmetry are documented problems of pairwise VLM judging, and v1's single-elimination tournament let one noisy call knock out the best frame. The feature weights are refit from the user's overrides, so the ranker learns the user's taste.

## P12 — Offline reverse geocoding with Photon (2026-11-02)

Photon 1.3 with the OpenStreetMap Europe dump, imported reverse-only (`-reverse-only -languages pl,en`), gives village/town, gmina, region and country names in Polish. It runs as a container on `127.0.0.1`. GeoNames `cities1000` (`reverse_geocoder`) covers the rest of the world. Nominatim was the heavier alternative. The GeoNames-only libraries miss Polish villages and return English admin names.

## P13 — A "burst" is the same scene from the same place, over minutes (2026-11-02, user)

In the user's sense, a burst is several shots of the same scene from the same place: retaken group photos, landscapes, a landmark shot again later. It is not the camera's burst mode, and the shots can be many minutes apart. So grouping has no short time cut-off, unlike v1, which compared only neighbours. Pairs inside one event and within a long window (default ~60 min) are compared. The similarity required grows with the time gap. GPS proximity, a geometric same-viewpoint check and overlapping face identities supply the extra evidence a long gap needs. Average linkage with a maximum diameter prevents chaining. Camera-burst metadata and naming only help: they seed groups and are never required.

## P14 — Absolute quality floor: bad photos go even without a better twin (2026-11-02, user)

v1 always kept at least one frame of every burst and never judged singles. v2 also archives photos that are bad on their own, whether that's a single or a burst whose every frame fails. Bad here means badly blurred, nearly black or white, accidental or failed. Cheap signals pre-screen, the expensive ones confirm, and the VLM gives reasons for borderline cases. The floor is calibrated per camera and era.

Because a bad photo may be the only record of something, value protects it: the only photo of an event or day, a rarely seen named person, or an old photo. Protected photos carry a warning and are never bulk-approved. Floor rejects are always reviewed, and they have their own false-archive metric.

## P15 — The repo stays public; photos purged from its history (2026-11-02, user)

v1's commit "better ai and burst" had committed two personal photos (`scripts/20220331_182829.jpg` with GPS, `scripts/tmp.jpg`), `tmp.jpg_original`, and three `error_log_*.json` files holding base64 images. The user chose to keep the repo public and remove them. History was rewritten with `git filter-branch --index-filter 'git rm --cached …'` over `master`, `delete_logs` and the `legacy-final` tag. Then `master` and `delete_logs` were force-pushed with leases, which also moved PR #1's head ref. A local bundle of the pre-rewrite state was kept outside the repo.

GitHub keeps serving the old commits by direct SHA until its garbage collection runs. Purging them sooner needs a GitHub Support request ("remove cached views"), quoting the old SHAs `911a5bd`, `e52ed56`, `ce2fea9` and `7e861bd`. The only fork (2018) predates these commits. The rule against it happening again is in `.gitignore`: no image, video, DB or log files are ever committed.

## P16 — Reference photos get category albums, learned from the user's own folders (2026-11-02, user)

Documents, receipts, serial-number labels, specs, screens, food and "to remember" shots aren't part of a trip or event. They go to category albums that extend the folders the user already keeps (`Pictures/Info/{Documents, Hardware, Paragony, Topos, …}`, `Pictures/Other/{Food, Meme, …}`). New categories (`Info/Dom`, `Info/Notatki`, `Info/Ekrany`, `Info/Zakupy`) follow the same layout.

Classification is a linear probe on SigLIP 2 embeddings, trained on those already-sorted folders, so it uses the user's own idea of each category. Depth planarity and text density are extra features, and the VLM confirms.

These kinds change other stages:
- **Grouping:** text must match before two document-like photos can be a burst, so the pages of one document are never culled as duplicates.
- **Quality floor:** reference photos are judged on legibility, not aesthetics.
- **Captions:** a full transcription, searchable through FTS5.
- **Sensitive documents** (IDs, cards, medical) are never moved automatically, and their numbers never go into file metadata.
- **Ties:** `memory` wins, because moving a memory out of its trip album is the costly mistake.

## P17 — The sample runs in the live-mode setup, with one runner image (2026-11-03)

The first sample run used the B50 for every vision model, one model at a time, next to the RAG models, plus the resident Qwen3.8 for the judge and captions. Nothing was evicted, so chat kept working, and live mode will run exactly this way (S10). It's also enough for small samples: 71 photos took about 6 minutes end to end. Bulk runs on the 4090 come later, through a llama-swap entry with declared VRAM.

There is one image, `mimir/omp:0.1`, for both the CPU stages and PyTorch XPU, built on the same base as mimir's depth image. It replaces the three images planned (CPU, CUDA, XPU) until the CUDA one is needed. The sample runs from a **copy** of the album under `/srv/mimir/cache/photos/sample/` with its own DB, so tests can't touch the mirror and the DB can be thrown away.

## P18 — Tags map phrase to phrase, one vector per synonym (2026-11-03, measured)

The VLM's free tags are matched to `vocab.yaml` by Qwen3-Embedding cosine against each label and synonym separately, with no instruction prefix, and need a cosine of at least 0.80. On the sample, one vector per vocabulary entry with an instruction-style query separated right from wrong mappings badly: "forest" → Forest scored 0.62, while "Poland" → Old town scored 0.38. Per-synonym matching gives right mappings ≥ 0.80 and wrong ones mostly below. Proper nouns go to a separate `names` list (places, venues) instead of being forced onto a concept, and generic tags ("outdoors", "nature") are dropped.

## P19 — The first review UI is a static page with exported labels (2026-11-03)

For labelling samples, `omp report` writes a self-contained folder: `index.html`, thumbnails and previews, and nothing loaded from the internet. It's copied to the workstation and opened there, which needs no server, no Serve port and no login while the pipeline is still changing every day. Labels live in the browser's localStorage and are exported as JSON, then imported with `omp labels`. The FastAPI + HTMX app with Tailscale Serve (S6) is still the plan for library-wide review and the daily digest.

The pages are per album, with an index of the albums in a run, so the same report covers any number of albums: the user wants a review page for every processed album (2026-11-03).

## P20 — Keep the frames that differ; zero-shot "accidental" is only a hint (2026-11-03, from the s1 labels)

The user keeps more than one frame of a burst when the frames differ: another moment, the subject facing the camera, a wider vista. In s1 every frame they archived was near-identical to a kept frame (DINOv2 ≥ 0.94) or clearly softer. So an extra frame is kept when it's below 0.95 similarity to every kept frame and not clearly worse, with no cap on the count. That took the false-archive rate on s1's album from 26 % to 7 % and cost 7 more false keeps. That trade is intended: a false keep costs some space, while a false archive loses a photo the user wanted.

SigLIP zero-shot "accidental" was wrong on 3 of 5 frames, so it no longer counts as a quality-floor reason. It only sends the photo to the VLM's worth-keeping check, and a photo flagged only by it is archived on a clear "no" (P(keep) < 0.2), never otherwise.

These rules were fitted to one album. They get re-checked on the next labelled albums before anything is tuned further.

## P21 — The review tool is a bundle at the root of each library (2026-11-03, user)

The pipeline will also run on external drives plugged into Mimir and on network shares, and the reviewer may be someone else on their own computer. So the review tool is a static bundle, `OrderMyPhotos/` at the root of the storage: an index, one page per album, 512 px thumbnails, and decision files. The open view shows the originals through relative paths. It needs no server, no install and no network. Per album the reviewer labels or trusts the automatic picks.

On external drives and shares, the page applies the result itself: File System Access API in Chrome/Edge, a hash check per file, then a move into `Archive/OrderMyPhotos/<path>`, logged. Deleting is the last step, in the page too: after review and apply, the reviewed archived photos are deleted only once the reviewer confirms, seeing their count, total size and list (user, 2026-11-03). On OneDrive, Mimir uploads the bundle album by album as albums finish, so review starts while the rest is still processing. The decisions come back through OneDrive's sync and are applied by `mimir-onedrive apply` (server-side, hash-checked), not by the reviewer's PC.

Folders of thousands of unsorted photos are reviewed as proposed albums: one unit in the index per event, with an editable name, merge, split and "leave in place". Applying a proposal creates the album folder (user, 2026-11-03).

This supersedes P19's static page for the sample, and the FastAPI app (S6) as the main review UI. The app is kept for the Tags and Albums views and the daily digest.

## P22 — What people and animals do is part of the photo (2026-11-03, user)

From the review: a frame where someone walks and one where they stand are different photos, and often the action of a person or an animal is the reason a photo is worth keeping. The s1 notes say the same ("subject facing better", "I like the motion"). A whole-frame embedding misses it: the subject is small, so the frames stay ~0.97 alike.

So S3 gets a **subjects** signal for every person and animal: a box (RT-DETR), a pose (ViTPose++ keypoints, people and animals), facing, cut-off and occlusion flags, and an action label (SigLIP 2 zero-shot on the subject's crop over a verb list). It is used in three places:
- **Keep count:** a frame counts as different when a main subject's pose or action changed, or someone entered or left, even if the whole frame is near-identical.
- **Ranking:** "the moment" features (the action at its peak within the burst, facing the camera, nobody cut off, sharp despite moving), weighted from the labels.
- **VLM judge and captions:** the judge compares the subjects' crops and says what each one does before it picks a frame. Captions list the subjects' actions, which become activity tags.

All three models are Apache-2.0 and run in `transformers` on the B50 like the other signals. They're measured on the s1 and s2 labels: how many false archives they prevent, and at what cost in false keeps.

## P23 — Extra frames are decided by a model fitted to the user's labels (2026-11-03, measured)

The s2 labels cover 467 photos in 5 albums, 272 of them non-best frames of bursts. They show that which extra frames the user keeps is mostly taste. Every signal tried separates the frames the user keeps from those they archive only weakly (AUC, 0.5 = chance):
- DINOv2 similarity to the best frame: 0.64;
- the VLM asked "keep this one too, or redundant?": 0.61 (it says "keep" to almost everything);
- people and animals (P22: count, pose change, action, facing): ~0.5, because detection flickers and people move a little between any two frames.

A logistic regression over the cheap features (similarity, sharpness against the best, quality and aesthetics differences, faces, burst size, rank) reaches **0.67 when each album is predicted by a model fitted on the other albums**. Adding the VLM answer or the subject features doesn't improve it, so neither is used for this.

The rule is: keep an extra frame at P(user keeps it) ≥ 0.30, and mark it "maybe redundant" below 0.50. That leans towards keeping, because false archives are the costly mistake. On the s2 labels (honest, leave-one-album-out) the false-archive rate went from 15 % to 9 %, while false keeps rose from 20 % to ~55 %. The "maybe" mark doesn't separate cleanly either: 48 of its 108 frames are user keeps. So the review page can archive the maybes in one click, but trusting an album keeps them.

The model refits on every run from all labels, so each labelled album improves it. P22's subject signal stays: it feeds the judge and the captions, and it will be retried once there are pose sequences to compare rather than pairs.

## P24 — Text pages: OCR saved with the photo, a `Dokumenty/` subfolder, document sets (2026-11-03, user)

From the review: "all of those are different pages of a book, … this one could be rotated, … if it's mostly text maybe create a txt file with OCR". Then: "run OCR on them and save the result with the photo", "pure text photos from the album should be moved to a subdirectory", and Unlimited-OCR (v1) as the fallback if v2 can't run.

Unlimited-OCR-2 does run: through Baidu's transformers code on the 4090 (mimir D23), ~58 s per page, with Polish read well. The `text` stage runs before grouping:
- orientation from the resident VLM, asked "upright?" per rotation (asked for the direction, it picked the wrong one, and the OCR read nonsense), with a lossless rotate proposal;
- OCR of the upright original;
- the text into the DB (FTS5), and a same-name `.txt` next to every photo that's mostly text when applied;
- text pages of an album proposed into its `Dokumenty/` subfolder.

Two photos of text are one burst only if their texts match (retakes of the same page). Otherwise they form a document set, whose pages are all kept. The subfolder name is configurable.

## Open — for the user

1. **Language** of titles, descriptions and tags: English, Polish, or both. Tags could carry both; descriptions need one.
2. **Archive and deletion:** is `Archive/OrderMyPhotos/` on OneDrive right, or should culled files leave OneDrive for a local archive on Mimir? How long before a delete?
3. **Edited copies:** keep both the edit and the original (the default), or only the edit?
4. **Live mode auto-apply:** which operation classes may skip review once proven safe (exact copies, metadata, bursts with a clear winner)?
5. **Video re-encoding:** yes or no, and is AV1 acceptable for playback on your devices?
6. **Folder policy:** confirm which top-level folders are `unsorted` (album candidates), `describe` or `ignore` (e.g. `Obrazy/Historia`, `Obrazy/Druk`, `Pictures/Private`, `Hosting`).
7. ~~**The public GitHub repo** had personal photos in its history.~~ Resolved by P15.
8. **Category albums:**
   - Are the default folders and names right (design.md S3b)? Should new ones be Polish (`Info/Dom`, `Info/Notatki`) or English like `Info/Documents`?
   - Should trip receipts and tickets go to Receipts (the default), or stay with the trip?
   - Any retention rules (e.g. suggest archiving receipts after 3 years)?
