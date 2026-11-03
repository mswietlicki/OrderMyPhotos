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
| Unlimited-OCR-2 (text pages, P24) | 4090, transformers 4.52.1 + torch 2.13 CUDA, through llama-swap | **~58 s per page** at ≤ 2560 px, 8.9 GB | Evicts Qwen3.8 while it runs. To try: base mode (one global view) instead of up to 16 tiles, for speed |
| Qwen3.8 "keep this one too?" (an experiment) | 4090 | 1.6 s per pair | AUC 0.61 against the labels: not used |

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
| Expression | **EmotiEffLib** 1.1 (`enet_b2`): happy probability, valence | successor of HSEmotion | 30 MB, Apache-2.0 | ONNX → CPU/any |
| Face quality | **eDifFIQA** `LSIbabnikz/eDifFIQA_M` | the tiny ONNX variant in opencv_zoo; CR-FIQA | 177 MB, CC-BY-4.0 | PyTorch/ONNX |
| Copy verification (crops, edits) | **SIFT + RANSAC homography** (OpenCV) on candidate pairs | ALIKED + LightGlue if SIFT misses filtered edits | — | CPU |
| Perceptual hash | pHash/dHash (`imagehash`) | | — | CPU |

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
