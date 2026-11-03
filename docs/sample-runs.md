# Sample runs

Each run of the pipeline on a sample, with measured numbers. The sample's labels (from the review page) are the ground truth for tuning, and every change to grouping, ranking or captions is measured here before it touches the library (design.md, Evaluation). Album, people and place names stay out of this file because the repo is public.

## Run s1: first end-to-end pass (2026-11-03)

**Sample:** one ski-touring album from February 2022. It has 71 photos from one phone (Samsung SM-G975F, 12 MP): the 50 left in the album and the 21 that v1 moved to `_archived/`, plus 16 v1 burst records and a machine-written `DESCRIPTION.md`. 54 photos have GPS. It spans one day of touring (64 photos), two short sessions the next morning, and one night shot. There are no videos, and no copies of these photos anywhere else on OneDrive. Few people appear, and none close up: the 8 faces found are 2–14 % of the frame height.

**Where:** a copy of the album at `/srv/mimir/cache/photos/sample/mirror/<same path as on OneDrive>`, with its own DB and cache, run with `docker compose --env-file sample.env run --rm omp <stage> --run s1` (mimir `stacks/photos`). The vision models ran on the **B50** next to the RAG models, and the VLM was the **resident** Qwen3.8 on the 4090. Nothing was evicted, so chat kept working throughout. This is the live-mode setup; the bulk setup (4090 for vision) isn't measured yet.

### Speed

| Stage | Device | Time for 71 | Rate | Notes |
|---|---|---|---|---|
| S0 inventory: walk, QuickXorHash, exiftool | CPU × 20 | 0.5 s | | The hash matches OneDrive's `quickxor` |
| S2 previews + sharpness grid, exposure, noise, pHash | CPU × 20 | 12.0 s | 5.9 photos/s | 3.2 s per photo per core: full 12 MP decode, Lanczos, three JPEG saves. ~8 h for 175k photos at this rate, so it needs tuning |
| DINOv2-B (448 px, aspect kept) | B50 XPU fp16 | 3.4 s | 21 img/s | |
| SigLIP 2 So400m NaFlex (≤ 576 patches) + zero-shot kinds | B50 XPU fp16 | 5.3 s | 13 img/s | |
| Q-ReAlign Mini, quality + aesthetics (1024 px) | B50 XPU bf16 | 30.0 s | 2.4 img/s | Two passes per image. Without `flash-linear-attention` / `causal-conv1d` it falls back to slow reference kernels |
| InsightFace `buffalo_l` (det 1024) | CPU | 8.3 s | 8.6 img/s | |
| Depth-Anything-V2-Small via llama-swap | B50 | 15.9 s | 4.5 img/s | 82 ms model time; the rest is HTTP + PNG |
| S4 grouping | CPU | 1.5 s | | 142 candidate pairs, 20 viewpoint checks |
| S5 ranking | CPU | 0.5 s | | |
| S5 VLM judge + floor checks | 4090 Qwen3.8 | 83 s | 1.6 s/call | 52 calls (12 bursts in both orders, plus 6 floor checks), ~2,000 prompt tokens per pair |
| S7 captions (PL + EN) | 4090 Qwen3.8 | 139 s for 33 | 4.1 s/photo | ~1,370 prompt + ~300 output tokens each; ~7 days for 150k on one slot, so the batch variant is needed |

### Grouping (S4) against v1

- **22 bursts, 60 of 71 photos in them**, and 11 singles. 4 events (gap > 2 h).
- **v1's 16 groups are all found again.** v1's groups 8 and 9, which are 2 s apart and were broken by one neighbour, are one burst of 6 in v2. Five v1 groups grew by one or two frames that v1 couldn't attach (a different shot came in between, or RMSE was over 0.30).
- **8 bursts v1 didn't have.** 2 of them are loose (margin 0.003 over the threshold), so they're flagged "loose" for review.
- **One long-gap burst:** 3 frames over **13 min**, confirmed by GPS (≤ 100 m) and SIFT inliers. That's the "same scene minutes apart" case. 18 of 20 long-gap candidates failed the viewpoint check, and 45 pairs failed the GPS distance.
- **DINOv2 CLS cosine** on this album: frames of the same shot 0.87–0.99, different scenes 0.0–0.55, with a grey zone of 0.6–0.85 that the time-dependent threshold (0.70 at 0 s → 0.90 at 60 min) decides. The patch-mean embedding and SigLIP separate less well (both stay high for any two snowy-forest shots).
- One pair 1 s apart (filenames 2 s apart) was first called a **copy** by mistake. Copies now require exactly the same capture time.

### Ranking (S5)

- **Keep 33, archive 38.** v1 archived 21. Both archive the same 9 photos. v2 keeps 12 that v1 archived and archives 29 that v1 kept, mostly because its bursts are bigger. Only the user's labels can say which is right. In near-identical bursts either frame may be fine, so the metric that matters is the **false-archive rate**.
- **VLM judge:** 12 bursts had close calls. On near-identical frames the verdicts are near-ties (51–58 %) both ways round, which is the intended behaviour: a tie leaves the feature ranking in place. It agreed decisively twice (66 %, 89 %) and never overruled the features.
- **Quality floor:** 6 photos fell below it. The VLM rescued one, 90 % "worth keeping": a close-up of skis in deep snow, a memory that zero-shot S3b had wrongly called a reference photo (see below). It confirmed 3 as not worth keeping: a dark night shot (83 % "no") and a two-frame burst from the next morning (16–18 % "keep"). That burst is archived as a whole, the case v1 could never handle.
- **Faces:** all small. "Face missing in this frame" is now counted only for faces ≥ 3 % of the frame height, because smaller detections flicker between frames. Blink detection needs faces ≥ 8 %, and there were none.

### Kinds (S3b) and captions (S7)

- **Zero-shot SigLIP kinds:** 61 memory, 4 reference, 6 junk. On the 33 kept photos, the VLM's `purpose` agrees on 31. Both find the same 2 reference photos, a **receipt** and a **price board**, which the VLM also transcribed (shop, address, amounts). The 2 disagreements are both **memories that zero-shot called reference or junk**: skis in deep snow, and people resting under umbrellas. That is the costly error (S3b: a memory moved out of its event album), so zero-shot alone isn't enough. The linear probe on the user's sorted folders is needed, plus "memory wins ties" and VLM confirmation. The receipt would go to the Receipts category album with the trip's tag (S3b placement rule).
- **Captions** are concrete and match the photos as far as the reasons and the text show: parked cars at a resort, a radio tower above the forest, a skier on a summit. They contain no flowery words, and the text in the photos (a ski-rental sign, number plates) is read. Number plates ended up in `text_in_image`. Whether they may go into file metadata is a privacy question for S9, like the Sensitive numbers. The VLM recognized the place as the ski area from the board and the receipt, and named it better than GeoNames' nearest town did (Photon should help).
- **Polish:** the per-photo captions read well, with a few awkward titles ("Skiowanie na szczyt"). The text-only album summary had two non-words ("turyków", "radiowozele"). This goes into the Polish bake-off (Gemma 4, Qwen3.6).
- **Tags:** the first mapping (one embedding per vocabulary entry, an instruction-style query, threshold 0.62) rejected obvious matches ("forest" → Forest at 0.617). Now each label and synonym gets its own vector, the tag is compared phrase to phrase, and the threshold is 0.80. That maps forest/conifers/pine trees → Forest and SUV → Car, and leaves "roof box", "umbrellas" and "shadow" for review. Proper nouns (the ski area, the mountain range, the region) go to a `names` list, and generic words ("outdoors", "nature") are dropped. SigLIP verification removed "sunny" five times, because its prompt "a photo of sunny" is poor: per-tag prompts are a to-do.

### Problems found and fixed in this run

- insightface 2.1 pulls in the GUI build of OpenCV (it needs X11 libraries), so the image swaps it back to the headless build.
- `tzdata` was missing: Python's `zoneinfo` needs it in the container.
- Q-ReAlign on XPU: `get_rope_index` failed with the attention mask present, because a boolean-mask index gave the wrong length on `torch 2.15.0+xpu`. With a batch of one there's no padding, so the mask is dropped.
- SigLIP: `logit_scale` and `logit_bias` needed `detach()` outside `inference_mode`.
- v1's captions turned out to cover about 590 albums, not 5 (legacy.md is corrected). Inventory now marks their fields as machine-written.

### Measured against the user's labels (2026-11-03)

The user labelled 70 of the 71 photos (`omp labels`, then `omp evaluate --run s1`):

| | s1 | Notes |
|---|---|---|
| Burst pairs < 60 s apart | precision **0.95**, recall **1.00** | 3 wrong pairs: one frame added to a neighbouring burst, and one pair 27 s apart at DINOv2 0.94 that the user sees as two shots to keep |
| Burst pairs ≥ 60 s apart | 2 predicted, **both wrong** | The 13-minute burst: the user split all three frames. GPS (5 m) and SIFT agreed it's the same place, but to the user they are three different photos |
| Best frame is one the user keeps | **18 / 20** bursts | |
| **False-archive rate** | **26 %** (11 of 42 keeps) | Far from the ~0 target. 3 come from grouping errors and 1 from a whole burst archived by the floor. The other **7 are extra frames the user keeps**: they kept 28 of 54 burst photos, the run 21 |
| False keeps | 2 | |

What the labels show:
- **The user keeps visibly different frames,** with notes like "I like the vista on the mountains here", "the subject is facing better" and "I like the motion". Every frame they archived inside a burst was either near-identical to the kept one (DINOv2 ≥ 0.94) or clearly softer. The extra frames they kept were 0.71–0.93 similar and about as sharp. The old rule (similarity < 0.90, score within 0.12, at most a third of the burst) archived them.
- **The zero-shot "looks accidental" signal is unreliable.** It flagged 5 frames, and the user kept 3.
- **The time-dependent similarity threshold is about right for short gaps.** The true pairs closest to it sit at +0.003; the false ones run from +0.06 up to +0.20, so no threshold separates them. For long gaps the user's idea is stricter than "same place, similar look". One album isn't enough to fit that.

## Run s2: rules from s1's labels, four more albums (2026-11-03)

**Changes:**
- An extra frame in a burst is kept when its DINOv2 similarity to every kept frame is < 0.95 and it isn't clearly worse (sharpness within 0.25 log units of the best, or score within 0.08). There's no longer a cap on how many frames are kept.
- Zero-shot "accidental" only sends a photo to the VLM's worth-keeping check. It no longer counts toward the floor, and a single only suspected of it is archived only on a clear VLM "no" (P(keep) < 0.2).
- The review page has one page per album plus an index of albums, bigger cards with a size slider, and the open view shows the burst's thumbnails (or a single's neighbours) to compare against. Labels already imported seed the pages of later runs.

**On s1's album** (the rules were tuned on it, so this is optimistic): false-archive rate **26 % → 7 %** (3 of 42; all three are preference calls between similar frames), false keeps 2 → 9, top-1 unchanged at 18/20. Grouping is unchanged.

**New albums,** chosen for variety and to be labelled the same way. No people or album names here; the DB has the paths.
- A 2009 town trip on an old compact camera (51 photos, low resolution): the floor per camera and era, landmarks.
- A 2015 wedding (87 photos, phone): faces close up, group shots, blinks, low light.
- A 2021 mountain trip shot with two cameras (136 photos, a phone and a compact): clock offset between devices, long-gap landmark bursts. Two of its photos are also stored in `Pictures/People` and `Pictures/Places`, which tests copies across folders.
- A 2022 family Easter (121 photos, phone): people indoors, food.

The first OneDrive sync was still running at the time, so the albums were picked from those already in the mirror.

**Results** (467 photos; there are no labels yet for the four new albums, so these are only the proposals):

| Album | Photos | In bursts | Keep / archive | Below the floor | Faces |
|---|---|---|---|---|---|
| 2009 town trip (HTC phone, 2 MP) | 51 | 26 | 38 / 13 | 5 | 35 |
| 2015 wedding (no camera EXIF) | 87 | 75 | 34 / 53 | 16 | 175 |
| 2021 mountains (phone + compact) | 135 | 101 | 78 / 57 | 3 | 50 |
| 2022 ski touring (s1's album) | 71 | 60 | 48 / 23 | 1 | 3 |
| 2022 family Easter (newer phone) | 121 | 111 | 66 / 55 | 3 | 181 |

- 106 bursts in 23 events. The biggest is 15 photos, which needs a look: party and family scenes are the hardest case for "same scene".
- The quality floor is calibrated per camera. The five cameras' median sharpness ranges from 7.8 (the wedding's camera without EXIF) to 10.1, so an old phone isn't judged against a new one.
- The VLM judge: 59 bursts and 84 worth-keeping checks, 249 calls at 1.4 s. Captions: 217 photos at 4.0 s.
- Speeds at this size, on the B50: DINOv2 34 img/s, SigLIP 2 18 img/s, Q-ReAlign 2.7 img/s, depth 104 ms per image, faces 5.2 img/s on the CPU (537 faces), previews 7.6 photos/s.

**Problems found and fixed:**
- **Camera-burst frames were called copies.** The wedding camera writes several frames in the same second (`…_180800.jpg`, `…_180759_1.jpg`), and four such pairs passed the near-copy test (same time, near-identical pixels). They were archived as "exact copy" with the `auto` tier, a false-archive risk. Two files in the same folder are now never copies: they're a burst. A copy left over is `review`, never `auto`.
- A face box reaching past the frame edge crashed the ranker on an empty crop. Boxes are clamped now.
- `docker compose run` in a script read the rest of the script from stdin, so only the first stage ran. Jobs now use `run -T … < /dev/null`.

**Not handled yet:** an exact copy stored in two folders is one photo with one verdict, so it shows up on both albums' pages with the same proposal. Per-file decisions for exact copies (keep the curated location, archive the other) are S1's job, still to build.

**Review bundle:** written at the library root (`OrderMyPhotos/`, 24 MB for 468 photos), and copied with the photos to `work/sample-library/` on the workstation, the way a drive would be handed over. Tested in a browser:
- the index lists 7 albums, all ready;
- the album pages start from the imported labels;
- the open view shows the original at full resolution through its relative path;
- trust from a page and from the index works, and "Export all decisions" collects every album.

## Labels for s2, and run s3: what to keep, text pages (2026-11-03)

The user labelled all 467 photos of s2. Their notes say which extra frames they keep ("I like the close-up", "subject facing better", "like the motion") and ask for text pages: "all of those are different pages of a book, … this one could be rotated, … OCR … save the result with the photo", "pure text photos from the album should be moved to a subdirectory".

**s2 against the labels:**

| | photos | burst pairs < 60 s P/R | ≥ 60 s P/R | keep user/run | false archives | false keeps | top-1 |
|---|---|---|---|---|---|---|---|
| all | 467 | 0.99 / 1.00 | 0.94 / 1.00 | 266 / 265 | **15 %** | 20 % | 88/104 |
| 2015 wedding | 87 | 1.00 / 1.00 | – | 40 / 34 | **30 %** | 13 % | 15/18 |
| 2021 mountains | 136 | 1.00 / 1.00 | 1.00 / 1.00 | 89 / 78 | 18 % | 11 % | 27/30 |

The bursts are right: the user changed almost none of them. The picks are where it fails: 40 photos the user keeps were archived, almost all extra frames of bursts.

**What was tried for the extra frames** (272 non-best burst frames, AUC 0.5 = chance; details in decisions.md P23):

| Signal | AUC | Note |
|---|---|---|
| DINOv2 similarity to the best frame | 0.64 | the single best signal |
| People and animals: count, pose change, action, facing (new `subjects` stage, P22) | ~0.50 | flagged 75 % of kept and 76 % of archived frames as "changed" |
| Qwen3.8 "keep this one too, or redundant?" | 0.61 | says "keep" to almost everything; 272 calls, 7 min |
| **Logistic regression on the cheap features, each album predicted from the others** | **0.67** | the same without the VLM or subject features: 0.67 |

**Run s3** (subjects, the text stage, the fitted model for extra frames, document sets):
- **Extra frames:** the fitted model keeps an extra frame at P ≥ 0.30, and marks "maybe redundant" below 0.50 (the page has "Archive the maybes"). False archives **15 % → 9 %**, false keeps 20 % → 51 % (honest, leave-one-album-out), top-1 unchanged. The wedding is still the weakest at 23 %.
- **Subjects:** RT-DETR + ViTPose++ + SigLIP 2 found 1,030 people and animals at 8.1 img/s on the B50. Most actions make sense (sitting, climbing, posing, holding a child, skiing), with a few wrong animals (3 "bears", 1 "zebra"). They're stored for the judge and captions, not used in the keep decision (P23).
- **Text pages:** 27 candidates, 24 mostly text, OCR at ~58 s per page on the 4090 (Unlimited-OCR-2, mimir D23). They include the guidebook: 6 phone shots and 4 camera shots of different pages, now **one document set of 10 pages, all kept**, proposed into `Dokumenty/`, with 2 pages flagged as turned. The VLM was first asked which way they were turned and said 90° clockwise. The OCR of that came out as Polish-looking nonsense ("nadzwyczel", "Puffowego Złobica"), while at 270° it reads correctly ("nadzwyczaj stromej płyty"). Asking "is it upright?" for each rotation picks 270° for both pages. There's one `.txt` per page in the bundle (`OrderMyPhotos/text/…`), to be written next to the photos when applied. Receipts come out of the OCR as HTML tables, which are turned into one line per item. Full-text search (FTS5) finds the guidebook pages by a peak's name.
- **Retakes of the same text:** the first text comparison (a character-level ratio, < 0.6 = different pages) turned 5 retakes of the same board at Easter into a document set. Word overlap against the shorter text separates them cleanly (retakes 0.84–0.99, different pages ≤ 0.48), so pages now need ≥ 0.7 to be one burst. That leaves 3 document sets.
- **Still open:** a photo stored in an album and in `Pictures/Places` is one photo with one verdict. The user kept the `Places` copy and archived the album copy (it isn't the best of its album burst). Decisions per file, with each curated folder judged on its own, are the next change to the data model.

## Run s4: proposals for unsorted photos (2026-11-03)

**Sample added:**
- two more 2019 albums;
- 483 unsorted photos from June–August 2019 (`Pictures/Photos/2019`);
- the `Pictures/People` folders (806 photos; faces only, P27);
- up to 40 example photos from each of 18 thematic folders (349; policy `reference` in the sample, so they only teach routing);
- in s4c, 250 WhatsApp images (all that arrived near a sample album's date or in 2019, plus random others).

The library index (599 dated albums, their content hashes and capture times) comes from the OneDrive manifest.

**Speed:**
- s4, 666 new photos:
  - previews 7.0/s;
  - DINOv2 36/s, SigLIP 2 17/s, Q-ReAlign 2.7/s, subjects 8.4/s, depth 6.4/s;
  - NudeNet 57/s and InsightFace 5.7/s on the CPU;
  - the judge: 455 calls at 1.4 s.
- s4c, 1,402 more photos:
  - DINOv2 40/s, SigLIP 2 20/s, Q-ReAlign 2.8/s, subjects 9.3/s, depth 4.7/s;
  - faces again for all 2,535 photos (the embeddings were missing in s4) at 5.7/s, 2,558 faces;
  - captions for 235 more kept photos at 4.0 s each;
  - the new kinds (`+zeroshot@2`) recomputed for 1,133 photos from their stored embeddings in seconds.
- **Text:** s4's first version OCR'd all 64 candidates: 31 min at 28.6 s per page, with Qwen3.8 evicted. With P34 the text stage only checks each candidate (42 in s4c, 1.6 s each) and tags the text pages "OCR needed".

**Picks against the labels** (the 5 labelled albums, 466 photos): unchanged from s3. Bursts < 60 s P/R 0.99/0.97, ≥ 60 s 0.94/0.91, false archives 9 %, false keeps 52 %, top-1 88/104.

**People (S8, P27):**
- 1,720 faces of usable size went into 108 clusters of 3+. 12 are named from the People folders, and the owner is found from the manifest.
- The album names (599, read in 10 calls of 18 s) named no further cluster yet. Only 7 albums are processed here, so few clusters recur in two of them.

**Proposals** for 733 unsorted photos (483 + 250 WhatsApp):

| Rule | Photos | Note |
|---|---|---|
| Copy of an album photo (hash) | 0 | |
| Downloaded (P31) | 41 | to `Other/Meme` 17, `Other/Funny` 17, `Albums/Screeshots` 5, `Other/Random` 2 |
| Messenger photo joins an album it looks like (P32) | 2 | sent the next day, SigLIP 2 cosine 0.90. Placed at its look-alike's moment, it joined that burst and the judge ranked it best |
| Other messenger photos | 207 | thematic folders, and 35 to category folders (screens 26, documents 5, labels 3, receipts 1) |
| Taken during an album (P25) | 46 | 42 of them in a shop 45–70 min before a trip album's first photo, 1.3 km away, inside the 2-hour margin |
| Reference photos to a category folder | 49 | 4 folders |
| New album (8+ kept photos) | 5 events | |
| Small events to thematic folders (P29) | 47 events | |

- **The messenger rule, measured on the library:** 63 % of all 3,717 WhatsApp images arrived during some album's days (or up to 3 days after), and the user put 1.9 % of those into that album. The first version let the day alone decide when the album had nothing to compare with, and joined 91 of the 250 to albums. Now only look-alikes join (≥ 0.80 within the days, ≥ 0.88 within 60 days).
- **Thematic folders** (all photos routed there in s4c): `Other/Random` 111, two undated albums 42 and 35 (their look is animals), `Places/<city>` 36, `Other/Meme` 29, `Other/Funny` 22, `Other/Food` 20, … An undated album named after a person first took 117 photos, because its average look is "a photo of someone". Albums named after a person (a People folder's first name or a name the album names mention) are now filled by faces only.
- **Album folders in the wrong place (P30):** the first rule found 452 on the manifest, 391 of them camera-upload day folders. With day folders, a device's day folders, sub-albums and curated folders excluded, it finds 36 with 8,238 photos: 18 phone-gallery albums, 14 older named event folders, a few named upload folders and a project folder. 4 of them merge into an album of the same name.
- **The namer against the user's own album names:** the 42 shop photos joining a trip album would have been named after the shop's district and the shopping. They show what the margin pulls in, but say little about naming. People named in the photos only reach the namer from this run on.

**OCR, three test pages read four ways (s4b):**
- **Flattening the page** (OpenCV outline and perspective warp) found no page outline in any of the three.
- **Single view instead of tiles:** no faster (28–81 s per page). Clearly worse on the 5504 px camera photo, mixed on the phone photo, the same on a receipt.
- **Qwen3.8 reading the photo directly:** 3–5× faster (7–22 s), with better diacritics. But it invents words (a non-word form of the peak's name, three times) and rewrites others ("odgraniczony" → "ograniczony"), so it isn't the OCR.
- So tiles stay, and the OCR is now a post-process (P34).

**Problems found and fixed:**
- Captions: a JSON answer cut off at the token limit (retried with 3× the budget, else that photo is skipped), and a caption of a photo now only in a People folder.
- Album dates: an album named "2018-2019 …" (not a date).
- Face embeddings weren't stored. `normed_embedding` is a property of InsightFace's face, not a dict key; the model version was bumped to redo them.
- The OCR candidates included the People folders and the thematic examples.
- A joining photo in a burst the reviewer labelled in an earlier run showed as a burst of its own. It now takes the reviewer's group.

**Still open:**
- the 2-hour margin before an album's first photo (the shop photos above);
- whether the two animal-looking undated albums should take photos by look;
- the OCR post-process over the kept pages;
- per-file decisions for copies in two curated folders.

## Runs s4d–s4f: sorting by sessions and events, orientation (2026-11-03)

The user labelled s4: 870 photos, 85 taken out of where S11 put them, and notes on where some belong ("Work", "Bilard z Kolegami", "hardware", "paragony", "rotate"). Their verdict: "Mostly I'm NOT happy with the sort" (decisions.md P35).

**Sorting against those labels** (the same sample, the labels imported):

| | s4 (photo by photo) | s4f (sessions, events, LLM decisions) |
|---|---|---|
| photos taken out that now go elsewhere | – (they were the complaint) | 84 of 85 |
| work and billiards photos (notes) in an album of their own | 0 of 9 (split over 4 thematic folders) | 9 of 9, in 4 albums |
| hardware, receipts (notes) | `Random`, `Documents` | `Info/Hardware`, `Info/Paragony` |
| the 42-photo pantry series next to a trip | joined the trip album | an album of its own (a shopping series) |
| photos given to the cat's album | 6, all taken out | 7, each within DINOv2 0.55 of one of its own; other cats to `Cats` |
| new albums / new folders | 5 / – | 22 / 5 |

- **Units:** 648 unsorted photos (85 of them taken out in review) became 95 sessions and 66 events.
- **Speed:** each event is one LLM call; with the static part of the prompt first, the server's cache brings that to 1.1 s per call (332 calls, 6 min).
- **What was tuned along the way:**
  - At first, single WhatsApp photos invented a folder each, about 50 one-photo folders. A new folder now needs 3 photos, and smaller ones are decided again.
  - "Koty" was created next to `Cats`.
  - Dates appeared inside names.
  - A folder the photos had been taken out of could be chosen again.
  - The owner's name ended up in an album name.
  - One- or two-photo walks near home became albums; an event that small now needs new people.
  - Decisions were made at temperature 0, so the runs repeat: at 0.2, the work presentation went to a new `Praca` folder in one run and to an album in the next.
- **Still open:** a few small new albums near home with 3 photos and no people ("… wieczór"), which the reviewer can turn down with "Not an album".

**Orientation (P36):**
- **One view:** a classifier on SigLIP 2 embeddings, trained on 1,500 photos turned four ways, got 63–69 % of held-out photos right and flagged 1 photo.
- **Four views:** looking at each photo in all four turns finds the turn 93.5 % of the time on 200 held-out photos turned at random, with no mistakes above a margin of 4.
- **Real turned photos:** on the user's noted photos and the turned text pages it points the right way every time, but with margins of 0.4–2.5. So the VLM checks what it flags.
- **Result:** 2,476 photos were looked at (9 min on the B50, plus 186 VLM calls). 93 were flagged and 37 confirmed turned, the user's noted photo among them (the VLM says upright: 0.001 as it is, 0.998 turned back). 33 of them carry a proposed turn in the review.

**Review:**
- one page per album or folder that receives photos, each incoming photo marked with the folder it comes from;
- two folders with one name get their parent in the title ("Dom (Info)", "Dom (Other)");
- pages of earlier runs are removed;
- `m` / `Shift+M` move a photo or a burst, `r` turns a photo, and all of it was tested on the workstation copy.
