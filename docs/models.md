# Models per stage

Shortlist from research done on **2026-11-02**. The licence and date of the key repos were checked against the Hugging Face API. Speeds are from papers and model cards unless marked **measured**. The sample-album bake-offs in [plan.md](plan.md) decide, and their results go here.

## Measured on Mimir (run s1, 2026-11-03, live-mode setup)

The vision models ran on the B50 next to the always-on RAG models, one at a time, and the VLM was the resident Qwen3.8 on the 4090. Details are in [sample-runs.md](sample-runs.md).

| Model | Runtime | Speed (measured) | Notes |
|---|---|---|---|
| DINOv2-B, 448 px long side | B50, PyTorch 2.15 XPU, fp16 | **21 img/s**, 34 img/s on 396 images (s2) | Same shot 0.87–0.99 cosine, different scenes 0.0–0.55 |
| SigLIP 2 So400m NaFlex, ≤ 576 patches | B50, fp16 | **13 img/s**, 18 img/s on 396 (s2) | Zero-shot kinds: 31/33 agree with the VLM; both errors call a memory non-memory |
| Q-ReAlign Mini 0.8B, 1024 px, quality + aesthetics | B50, bf16 | **2.4–2.7 img/s** (two passes) | Reference kernels (no `flash-linear-attention`). Drop the attention mask on XPU, see sample-runs.md |
| InsightFace `buffalo_l`, det 1024 | CPU, onnxruntime 1.31 | **8.6 img/s** with few faces, 5.2 img/s with 537 faces in 396 (s2) | insightface 2.1 |
| Depth-Anything-V2-Small via llama-swap | B50 | **82–104 ms** per image (4.5–5.9 img/s end to end) | 768 px input |
| Qwen3.8-27B judge: 2 images at ~1 MP, 1 output token | 4090 | **1.4–1.6 s** per call | ~2,000 prompt tokens. Logprobs work through llama-swap with MTP on |
| Qwen3.8-27B caption: 1 image, JSON schema, PL + EN | 4090 | **4.1 s** per photo | ~1,370 prompt + ~300 output tokens |
| Qwen3-Embedding-4B, tag → vocabulary | B50 | (cheap) | Phrase-to-phrase per synonym, cosine ≥ 0.80 |
| RT-DETR R50 + ViTPose++ base + SigLIP 2 crops (subjects, P22) | B50, PyTorch XPU | **8.1 img/s** (1,030 people and animals in 467 photos) | Post-processing on the CPU (XPU boolean-mask bug). Didn't separate the user's keep/archive (P23) |
| Unlimited-OCR-2 (text pages, P24) | 4090, transformers 4.52.1 + torch 2.13 CUDA, through llama-swap | **~58 s per page** at ≤ 2560 px in the first test, **28.6 s per page** over 64 photos in s4, 8.9 GB | Evicts Qwen3.8 while it runs. To try: base mode (one global view) instead of up to 16 tiles, for speed |
| Qwen3.8 "keep this one too?" (an experiment) | 4090 | 1.6 s per pair | AUC 0.61 against the labels: not used |
| NudeNet 3.4.2 (320n ONNX), full nudity (P26) | CPU, onnxruntime | **57 img/s** on 1,132 photos (s4), 45 img/s on the People folders | Exposed/covered body parts. Licence: MIT on PyPI, AGPL-3.0 on GitHub; private use either way |

The library is private and non-commercial, so non-commercial licences (InsightFace weights, pyiqa, Depth-Anything-V2 Base/Large) are acceptable. They are marked anyway in case anything is ever shared.

## Where things run

| GPU | Free for this project | Use |
|---|---|---|
| RTX 4090 24 GB | All of it during batch runs (llama-swap evicts Qwen3.8 for the vision server, ~10 s to swap back) | Bulk vision signals over the whole library; the VLM (judge + captions); video decode, encode and keyframes (NVDEC/NVENC) |
| Arc Pro B50 16 GB | ~8 GiB beside the always-on RAG models (7.6 GiB) | **Live mode** vision signals, so the chat model is never evicted; depth; extra capacity during bulk runs |
| CPU 12C/24T | ~20 worker processes | decoding, hashing, exiftool/ffprobe, sharpness maps, MediaPipe on face crops, SIFT checks, clustering, grouping |

## Signals (S3)

| Need | Primary | Alternatives / notes | Size, licence | Runtime → GPU |
|---|---|---|---|---|
| Near-duplicate embedding | **DINOv2-B** `facebook/dinov2-base` | **DINOv3-L** `facebook/dinov3-vitl16-pretrain-lvd1689m` is better on instance retrieval (+7–11 points over v2 in the paper) but gated, under Meta's custom licence (fine privately). Bake-off on the sample album's bursts and copies | 86M, Apache-2.0 / 300M, custom | PyTorch → 4090 (bulk), B50 (live) |
| Semantic embedding, zero-shot tags, search | **SigLIP 2 So400m NaFlex** `google/siglip2-so400m-patch16-naflex` | Multilingual text tower (Polish works, a bit weaker than English). Later: `Qwen/Qwen3-VL-Embedding-2B` (Apache-2.0, 30+ languages, much heavier). Skip PE-Core: English-only text | ~1B, Apache-2.0 | PyTorch → 4090 / B50 |
| Technical + aesthetic quality | **Q-ReAlign Mini** `q-future/Q-ReAlign-Mini-0.8B`: one pass gives both scores | **Q-ReAlign Pro** `q-future/Q-ReAlign-Pro-9B` re-scores only the top candidates of each group. TOPIQ-NR (pyiqa) on subject crops. All are trained on KonIQ/SPAQ/AVA, so the published scores are in-domain | 0.8B (2.2 GB) / 9B (18.8 GB), Apache-2.0, released 2026-06 | transformers ≥ 5.2 → 4090 (~27 img/s Mini per the card) |
| Subject mask: saliency | **BiRefNet** `ZhengPeng7/BiRefNet` | Only for photos in groups | 0.2B, MIT | PyTorch → 4090 / B50 |
| Subject mask: depth | **Depth-Anything-V2-Small** (already running on the B50, ~80 ms **measured**) | **DA3-Small / DA3-Base** `depth-anything/DA3-SMALL` (Apache-2.0, 2025-11) to compare. V2 Base/Large are CC-BY-NC | 25M | PyTorch XPU → B50 |
| Sharpness, motion vs defocus, exposure, noise | **Classical**: tiled Tenengrad/Laplacian at full resolution inside the subject mask, structure-tensor anisotropy, histogram clipping, MAD noise | No maintained blur-map model exists. DMENet and Son et al. 2018 are TF1, so skip them | — | CPU (numpy/OpenCV) |
| Face detect + landmarks + identity | **InsightFace `buffalo_l`**: SCRFD-10GF at 1280 input (or tiled for group shots), 2d106 landmarks, pose, ArcFace R50 | `antelopev2` (R100). LVFace (ICCV 2025, non-commercial). Immich uses `buffalo_l` | 326 MB ONNX, non-commercial weights | onnxruntime CUDA → 4090; OpenVINO EP → B50 |
| People and animals: boxes | **RT-DETR** `PekingU/rtdetr_r50vd_coco_o365` (COCO classes: person, dog, cat, horse, bird, cow, sheep, …) | `PekingU/rtdetr_v2_r50vd`; OWLv2 / Grounding DINO for open-vocabulary animals | ~43M, Apache-2.0 | transformers → B50 / 4090 |
| Pose (people and animals) | **ViTPose++** `usyd-community/vitpose-plus-base` (MoE, a head per dataset: COCO body; per the paper also AP-10K / APT-36K animals, index to verify) | `vitpose-plus-small` for speed; RTMPose (mmpose) via onnxruntime | 0.1B, Apache-2.0 | transformers → B50 / 4090 |
| Action of each subject | **SigLIP 2** zero-shot on the subject crop over a verb list (EN + PL), plus the VLM's `subjects[]` in captions and the judge | a pose-sequence classifier only if zero-shot falls short | (already loaded) | B50 |
| Blink, smile | **MediaPipe Face Landmarker** on face crops: 52 blendshapes (`eyeBlinkLeft/Right`, `mouthSmile*`) | Cross-checked with the eye aspect ratio from the 2d106 landmarks. Thresholds calibrated on the sample | small, Apache-2.0 | CPU |
| Expression | **EmotiEffLib** `enet_b0_8_va_mtl` (AffectNet: 8 expressions + valence/arousal), in use (P50): P(happiness) per face, on a square crop of the face box. **Measured** on s6's 287 faces against the local VLM ("is this person smiling?"): AUC 0.85; at P ≥ 0.5 the VLM agrees 99 %. ~10 s for 517 photos | `enet_b2_8` (30 MB) agreed the same (AUC 0.84), so the smaller one, which also gives valence | 16 MB, Apache-2.0 | ONNX → CPU |
| Face quality | **eDifFIQA** `LSIbabnikz/eDifFIQA_M` | the tiny ONNX variant in opencv_zoo; CR-FIQA | 177 MB, CC-BY-4.0 | PyTorch/ONNX |
| Copy verification (crops, edits) | **SIFT + RANSAC homography** (OpenCV) on candidate pairs | ALIKED + LightGlue if SIFT misses filtered edits | — | CPU |
| Perceptual hash | pHash/dHash (`imagehash`) | | — | CPU |

## Picking the frame: what the research says (2026-11-05)

What the literature and the culling tools say about choosing the best of a series, and how it bears on OrderMyPhotos. The numbers marked "s7" are from the reviewer's own picks (sample-runs.md, s7 analysis).

- **[Automatic Triage for a Photo Series](https://pixl.cs.princeton.edu/pubs/Chang_2016_ATF/index.php)** (Chang et al., SIGGRAPH 2016): 5,953 series of personal photos, with pairwise preferences from MTurk.
  - What raters gave as reasons, against a photo: blur, dark lighting, washed-out colour, distracting foreground or background. For one: sharper, clearer and closer, more detail, brighter colour, often a wider shot.
  - For portraits: "smiling face", "closer face", "face angle" and "eyes".
  - The photos' owners chose the crowd's pick in 358 of 551 series (65%); where they didn't, the crowd was split too.
  - A generic aesthetics model (RAPID, trained on AVA) and composition rules came out close to random between similar photos. Sharpness did well. A Siamese CNN reached 73% on pairs with ≥ 70% agreement.
- **[Top Shot](https://research.google/blog/top-shot-on-pixel-3/)** (Google, Pixel 3):
  - per face: eyes open, smiling, and emotional expression (amusement, surprise), combined over the faces with a small additive model;
  - without faces: subject motion saliency, global motion blur, and exposure and focus;
  - the whole fitted to the frames that hundreds of volunteers chose.
  - That's the shape of rank's score, with a few weights fitted to labels.
- **[Best Frame Selection in a Short Video](https://openaccess.thecvf.com/content_WACV_2020/html/Ren_Best_Frame_Selection_in_a_Short_Video_WACV_2020_paper.html)** (Ren et al., WACV 2020): 11,000 clips with the best frame marked. The context of the other frames matters to the choice, so frames should be compared within their series, not scored alone.
- **[FGAesthetics / FGAesQ](https://arxiv.org/abs/2603.03907)** (CVPR 2026): fine-grained aesthetics within series of near-identical photos, 10,028 series ranked pairwise, burst photos among them.
  - FGAesQ, a CLIP ViT-B/16 model, picks the better of a natural pair 78% of the time, against 71% for Q-Align (8B).
  - The [weights are on Hugging Face](https://huggingface.co/yzc002/FGAesQ), with no licence stated.
  - **A candidate for bursts:** small, made for exactly this, and to be measured against the s7 picks.
- **[HumanAesExpert](https://arxiv.org/abs/2503.23907)** (2025): aesthetics of photos of people. Its scores cover the face (brightness, clarity, skin, structure, contour), outfit, body shape, looks and environment, but not pose or expression.
  - [HumanAesExpert-1B](https://huggingface.co/KlingTeam/HumanAesExpert-1B) is MIT, 0.9B parameters (InternViT-300M + Qwen2-0.5B).
  - A candidate for people frames, also to be measured on s7.
- **[Visual Aesthetic Benchmark](https://icml.cc/virtual/2026/68543)** (ICML 2026): the best of 20 frontier multimodal models chose both the best and the worst image of a set in 26.5% of tasks, against 68.9% for human experts. Fine-tuning a 35B model on 2,000 expert examples brought it near a 397B one.
  - So the VLM judge isn't an aesthetic expert out of the box. The reviewer's own labels (s7: 1,120 verdicts) are worth more than a better prompt alone.
- **Personal taste:** few-shot personalised aesthetics ([task vectors](https://arxiv.org/abs/2407.07176), ECCV 2024) is an open problem; one evaluation found it didn't beat general predictions on average. Culling tools (Aftershoot) learn from the photographer's own culls over time. That's the extra-frame model carried from one session to the next.
- **Albums, not frames:**
  - [Event-specific image importance](https://openaccess.thecvf.com/content_cvpr_2016/html/Wang_Event-Specific_Image_Importance_CVPR_2016_paper.html) (CVPR 2016): what matters in an album depends on the kind of event.
  - Album summaries trade off [quality, diversity and coverage](https://archives.iw3c2.org/www2011/proceedings/companion/p127.pdf) of people, events and places.
  - Facebook's summary patent picks a photo per group of people.
  - That supports keeping a photo of every person at every event.
- **Interestingness** ([Gygli et al., ICCV 2013](https://openaccess.thecvf.com/content_iccv_2013/html/Gygli_The_Interestingness_of_2013_ICCV_paper.html)) comes from unusualness, aesthetics and emotional arousal. Memorability models (ResMem) exist, but they score a photo alone, not a frame against its burst.

**Against the s7 picks (within bursts):**
- **Expression intensity:** the faces' emotional arousal (EmotiEffLib, P50) was higher in the kept frames in 66% of bursts, and higher in the reviewer's frame where their pick differed from the pipeline's in 65%. Valence, the smile, was higher there in only 41%. So intensity of expression, not happiness alone, is what the reviewer keeps.
- **Eyes open** (eye openness, already measured): higher in the reviewer's frame in 79% of the bursts where they differed (14 bursts). rank doesn't use it yet.
- **Face angle and face size:** no effect.
- Pose measures from the keypoints didn't predict the picks (sample-runs.md, s7 analysis).

## Bursts, events and albums: what the research says (2026-11-05)

How others group a personal library into bursts, events and albums, and how that compares with `group.py` and S11. Today: bursts are DINOv2 similarity with a threshold that rises with the time gap (0.70 to 0.90 within an hour), with GPS and a viewpoint check (SIFT) for longer gaps. A group event breaks at a fixed 2 h gap. S11 builds sessions and events from time, place and people, and away-from-the-rolling-base trips.

- **Adaptive time gaps** ([PhotoTOC](https://typeset.io/pdf/phototoc-automatic-clustering-for-browsing-personal-274halnisp.pdf), Platt et al., Microsoft Research):
  - A gap is an event boundary when it's much longer than the gaps around it, compared on log time. Its [patent](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/7076503) gives the rule: log X = log 17 + the mean log gap of the 20 gaps around it.
  - A fixed threshold fails because a wedding's events are minutes apart and a holiday's hours apart.
  - People also differ in how fine their events are: a several-day trip as one event or one per day ([adaptive event clustering](https://koreascience.or.kr/article/CFKO200625752346270.page)). That's what the reviewer's albums show, and what P37–P41 learnt.
  - **A candidate for `group`'s fixed 2 h event gap**, to measure against the s7 albums.
- **Sub-events** ([Stopwatch HMM](https://data.vision.ee.ethz.ch/cvl/datasets_extra/pec/), Bossard et al., ICCV 2013): an event is a sequence of sub-events, with transitions that depend on the time gap. The PEC dataset has 807 collections in 14 kinds of social event, and recognising the kind is still hard (56%). It's the reviewer's rule (P41) that an event with other people, at another place, inside a long album is an album of its own.
- **Place:**
  - [PhotoCompas](https://bibbase.org/network/publication/naaman-song-paepcke-garciamolina-automaticorganizationfordigitalphotographswithgeographiccoordinates) (Naaman et al., JCDL 2004) groups by time and GPS into a hierarchy of place names (reverse geocoding).
  - Google and Apple find trips as [time away from home](https://5minutephotos.substack.com/p/trips-with-the-new-apple-photos). Apple's misfires on former homes, which is why the reviewer dropped "home" for a rolling base (P38).
  - Open-source photo apps use DBSCAN on GPS (5 km), falling back to months.
- **Near-duplicates are a matter of degree:**
  - [California-ND](https://www.scitepress.org/Papers/2014/47377/) (Vonikakis et al., 2014) is a personal travel collection where 10 people marked near-duplicates, kept as a probability rather than yes or no; people disagree on them.
  - Patents keep "series" (several shots of one scene) apart from duplicates.
  - The usual design is a cheap hash pass for copies, then DINOv2 embeddings with a threshold for bursts. DINOv2 tells a burst from different photos better than CLIP. That is `group`'s: copies (P54), then bursts.
  - DINOHash (2025, DINOv2 tuned for perceptual hashing) could replace pHash + DINOv2 for the copy check if needed.
- **Albums as summaries:**
  - an album is chosen for quality, diversity and coverage of people, events and places ([Sinha et al., WWW 2011](https://archives.iw3c2.org/www2011/proceedings/companion/p127.pdf));
  - what matters depends on the kind of event ([Wang et al., CVPR 2016](https://openaccess.thecvf.com/content_cvpr_2016/html/Wang_Event-Specific_Image_Importance_CVPR_2016_paper.html));
  - one patent picks a photo per group of people.
  - So: every person at every event (P55), and a few per sub-event rather than many of one moment.
- **Memories with LLMs:** recent work answers questions over a personal archive with an LLM that retrieves by time and place: [Memory-QA](https://aclanthology.org/2025.emnlp-main.1234.pdf) (EMNLP 2025), [OmniQuery](https://dl.acm.org/doi/10.1145/3706598.3713448), and [OpenLifelogQA](https://arxiv.org/pdf/2508.03583), 18 months of lifelog with 27k event descriptions and a Qwen baseline. That's S11's captions, places and events, reused later for search ("when were we at the lake with …").
- **Not found:** a published method for building albums from VLM captions like S11's, or a benchmark of DINOv2 against CLIP for bursts in personal photos. Thresholds stay tuned on the reviewer's own albums.

## VLM: judge (S5) and captions (S7)

| Model | Why | Fit | Status |
|---|---|---|---|
| **Qwen3.8-27B** UD-Q4_K_XL + mmproj (installed) | Best quality of the open options; fully supported in llama.cpp b12033; JSON via `response_format: json_schema`; thinking off via the `:instruct` alias | 22.2 GB at 64K + MTP. A batch variant uses several slots with small contexts | ✅ running, 118 tok/s **measured** (text). Vision latency not yet measured |
| **Qwen3.6-35B-A3B** UD-IQ4_XS (17.7 GB) | MoE with 3B active, likely 2–3× faster on captions | fits the 4090 | bake-off |
| **Gemma 4 26B-A4B** (ggml-org GGUF) | MoE, 140+ languages, maybe better Polish. Fixed image budgets (70–1120 tokens), so `-ub` must cover them | fits the 4090 | bake-off |
| Qwen3.5-9B / 4B Q4–Q8 | Could caption on the B50 in parallel, but the B50's memory bandwidth (224 GB/s) makes decode slow | 5.7–9.5 GB | only if the B50 is otherwise idle |

**Image cost:** Qwen3.x uses ~1,000 tokens per ~1 MP image (patch 16, merge 2), so previews are sent at ~1 MP. MTP with images is supposed to work since llama.cpp PR #22838 (May 2026); to be measured.

**Judge protocol:** each pair is asked in both orders. The verdict is read from the `1`/`2` token logprobs, not the text, and the two orders are averaged. With 3–4 contenders the results are aggregated by Bradley–Terry (`choix`). Pairwise VLM judging is known to be position-biased and not always symmetric, which is why the swap is mandatory. The Phototriage dataset (15.5k personal photos in 5.9k series, with pairwise preferences) can tune the fusion weights; the published ceiling is ~75% agreement.

## Tags, text search

| Need | Choice |
|---|---|
| Controlled vocabulary tags | SigLIP 2 zero-shot over `vocab.yaml` (English + Polish labels; calibrated per-tag thresholds), unioned with VLM tags mapped to the vocabulary |
| Normalizing tags, text search over captions | **Qwen3-Embedding-4B** + **Qwen3-Reranker-0.6B**, already on the B50 |
| Fixed-tagger alternative | RAM++ (4,585 tags, Apache-2.0, 2023), only if the zero-shot plus VLM approach falls short |

## People (S8)

The identity embeddings come from `buffalo_l`. Clustering uses HDBSCAN or Chinese Whispers for the batch pass, then incremental nearest-centroid assignment (Immich does incremental DBSCAN with min 3 faces at distance 0.3–0.7). Faces below a size, detection-score and eDifFIQA threshold are left out of clustering.

## Video (S12)

| Need | Choice | Runtime |
|---|---|---|
| Probe | `ffprobe`: duration, codec, bitrate, resolution, rotation, creation time, QuickTime GPS | CPU |
| Decode / keyframes | ffmpeg with NVDEC (4090) or VA-API/QSV (B50); scene-change keyframes (PySceneDetect or ffmpeg `select=gt(scene,…)`), capped per minute | GPU decode |
| Copies of a video | Keyframe DINO embeddings aligned in time, plus a **Chromaprint** (`fpcalc`) audio fingerprint, for re-encodes, trims and WhatsApp versions | CPU + GPU |
| Junk clips | duration, darkness and flat depth on keyframes, Q-ReAlign on keyframes, optical-flow shake, audio loudness | mixed |
| Speech | Whisper-class STT on the B50 (mimir Phase 4 "speech-to-text", to be chosen there; Polish is a requirement) | B50 |
| Captions | the same VLM, given 4–8 keyframes in one request plus a transcript excerpt | 4090 |
| Re-encode (optional, lossy) | ffmpeg AV1 or HEVC on **NVENC** (Ada has AV1) or the B50's AV1 encoder; **VMAF** (libvmaf) gates the quality | GPU encode |

## Not used, and why

- **Global Laplacian at 400 px:** v1's only sharpness signal; it rewards noise and punishes bokeh.
- **The LAION aesthetic predictor (`laion_aes`, 0.665 on AVA) and aesthetic-predictor-v2-5** (AGPL, no benchmarks).
- **DeQA-Score:** pinned to transformers 4.x and superseded by Q-ReAlign.
- **VisualQuality-R1 / Q-Insight with reasoning:** more than 2 s per image, too slow for bulk scoring.
- **Single-elimination tournaments and Elo** (order-dependent); Bradley–Terry is used instead.
- **Moondream 3** (BSL licence, no llama.cpp support), SmolVLM2 (weak Polish), InternVL 3.5 (no llama.cpp support yet).
- **Any cloud API for pixels.**
