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

### Next

1. **You label the sample** on the review page (`work/sample-s1/index.html` on the workstation). Fix the bursts, mark the frames to keep, and mark the photos that are bad on their own, then export the labels. `omp labels --file labels-s1.json` imports them.
2. Calibrate against the labels: the similarity-over-time curve, the diameter, the GPS and viewpoint rules, the keep margin, and the floor thresholds. Report pairwise precision and recall for short and long gaps, and the false-archive rate.
3. A second sample with people close up (group shots, blinks), plus a slice of the unsorted phone stream with videos.
4. Bulk setup: vision on the 4090 through a llama-swap entry, the `qwen3.8-27b-batch` variant, and faster previews.
