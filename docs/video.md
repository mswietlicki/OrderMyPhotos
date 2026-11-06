# Videos (S12 in detail)

Status: **proposal, 2026-11-06.** Nothing here is built. It expands design.md S12 with what the user asked for on 2026-11-06:
- faces and people;
- transcripts with speakers;
- clips cut by scene, quality and what changes;
- descriptions and tags;
- triage of whole videos and of the moments inside them;
- re-encoding;
- colour for raw footage;
- videos placed in the photo albums they belong to.

Once the open questions at the end are answered, the summary goes back into design.md S12, plan.md Phase 6 and models.md "Video (S12)", and the decisions into decisions.md.

## The library's videos (survey, 2026-11-06)

The numbers come from the `onedrive` session's DB plus one exiftool pass over every video's headers (read-only, 1 CPU, 32 s).

| | Value |
|---|---|
| Files the inventory calls video | 4,778. **296 of them are TypeScript files** (`*.d.mts` in a `node_modules` folder under `Dokumenty/`), so V0 sniffs the container and the policy leaves `**/node_modules/**` |
| Real videos | 4,482 files, 4,198 distinct (284 exact copies), **358 GiB, 53.9 hours** |
| Length | median **17 s**. 10% are ≤ 3 s, 90% ≤ 84 s, 99% ≤ 8 min, and the longest is 75 min. 1,448 files are ≤ 10 s (2.1 h in total). 84 files over 5 min hold 15.3 h |
| Codecs | H.264: 3,620 files, 42.7 h, 248 GiB. HEVC: 671 files, 10.0 h, 105 GiB. MJPEG AVI from old compacts: 122. MPEG-4 Part 2: 40. AV1: 11 |
| Resolution | 1080p class: 2,865 files, 37.8 h. 4K: 149 files, 2.1 h. 720p: 247. Smaller: 1,221 files, 10.9 h (WhatsApp, old phones, memes) |
| Bitrate | median 14 Mb/s, 90th percentile 21 Mb/s |
| HDR | 134 HLG clips (BT.2020, HEVC), 2.0 h, all from the newest phone. No PQ |
| RAW / log | **None in OneDrive**: no ProRes, BRAW, CinemaDNG or log profiles. The originals from the drone and the action cams are on the NAS (next section). The 4K Panasonic compact (116 files, 1.6 h, 34 GiB) is the closest thing here |
| Frame rate | 157 files at 50–60 fps, 22 slow-motion at ≥ 100 fps |
| Audio | 3,856 AAC. 606 have no audio track or one exiftool can't name |
| Place | 2,020 files have GPS |
| Time | 2,949 distinct videos have a QuickTime creation date (UTC). **991 have none**: the date is zeroed by editors, WhatsApp and converters. 250 more have only a day from the file name |
| Written by ffmpeg-based software | ~1,000 files tagged `Lavf…` or HandBrake: exports, WhatsApp, downloads. 25 of them are 4K HEVC at ~103 Mb/s with no audio. Those are **drone clips**: DJI's firmware writes the same `Lavf56.15.102` tag (see the NAS survey). They hold 52 GiB in 1.2 h, and most are stored twice (gallery and album), so S1 already archives one copy |
| Where (hours) | Samsung Gallery 24.9, Albums 15.0, `Obrazy/Other` 3.8, `Pictures/Photos` 2.5, Camera Roll 1.9, WhatsApp 1.1, the camera folder under `Video/` 1.1 |
| Near photos | Of the 2,055 unsorted videos with a time, **53% have a photo within 10 min, 73% within 1 h** and 90% within 6 h |
| Motion Photos | 1,751 photos carry an embedded clip |

What this means:
- **The work is small in hours and big in bytes.** 54 h of footage is about a day of audio and a few hundred thousand frames at 1 fps. The cost is reading 358 GiB and the final encode, not the models.
- **Most clips are short phone takes.** Hard cuts exist only in edited exports. "Slicing" a phone clip means trimming its ends and cutting the dead middle parts, not detecting shots.
- **Videos follow the photos.** Three quarters of the unsorted ones have photos within the hour, so S11 can place them with the photos' events. The 991 timeless ones need other clues (V8).
- A folder named `Video/Private` holds one 1.3 h video under the default policy (`unsorted`). `Pictures/Private/**` is `leave`, and this folder probably should be too (question 7).

## Raw footage on the NAS (survey, 2026-11-06)

The user's drone and action-cam footage is kept in raw storages (user, 2026-11-06). The pipeline is to run on those as well. The first example is the `RAW` folder on the Synology `apollo` (`\\apollo\nas\RAW`).

The survey read it from the workstation: ffprobe headers for every file, GoPro's `udta` settings, the DJI `.SRT` field names, and luma/saturation statistics of 116 keyframes. Only numbers were read: no frames were looked at, and no file was copied.

| Camera | Files | Hours | GiB | Format | Extras |
|---|---|---|---|---|---|
| GoPro HERO3 | 946 | 61.0 | 874 | 1080p60 (some 30, 48 and 240 fps, 1440p), H.264 8-bit, full range, ~30 Mb/s | No telemetry, no stabilisation |
| DJI drone | 614 | 18.2 | 735 | 4K (some 2.7K) HEVC, 8-bit, 30 fps, 50–100 Mb/s, **no audio** | 119 `.SRT` telemetry files (GPS, altitude, ISO, shutter, white balance, colour mode), only 4 of them next to their MP4. The colour mode is always `default` |
| GoPro HERO7 | 162 | 11.0 | 219 | 1080p, 2.7K and 4K at 60 fps, H.264 (8 in HEVC), ~45–60 Mb/s | GPMF track (gyro, accelerometer, GPS). HyperSmooth on in 161 files. Protune on in 37, but the colour is always "GoPro" (not Flat), with auto white balance. 37 RAW-audio `.WAV` files and `.LRV` proxies beside the MP4s |
| Garmin VIRB | 51 | 1.6 | 20 | 1080p60 H.264 | — |
| Edited or re-encoded (HandBrake, MainConcept, …) | ~55 | ~3 | ~20 | Exports | — |
| **Total** | **1,828** | **95.5** | **1,868** | | Plus 2,238 JPEGs (11 GiB) from a Panasonic compact, the GoPro and the drone: photos for the photo pipeline |

What this means:
- **"Raw" here means unedited camera originals, not log or RAW codecs.** Everything is 8-bit Rec.709: the DJI colour mode is `default` and the HERO7's colour is "GoPro". So colour work means correcting each shot and **matching cameras** within an event, not decoding log.
  - Across the sampled keyframes, the HERO3 and Garmin look flatter and less saturated than the HERO7. The median spread between the 10th and 90th percentile of luma is 103 and 88 against 140 (the drone: 125), and the median saturation is 6 against 11.
  - That is partly the content (snow). Even so, those two cameras get the strongest correction.
- **There is far more of it, and far more to cut.** It holds 1.8× the hours of the OneDrive videos and 5× the bytes. The takes are continuous: a helmet cam over a whole ski run or a lift ride, a drone from takeoff to landing. A guess: 10–25% is worth keeping.
- **GoPro splits long recordings into chapters**: 61 recordings are spread over 152 files, up to 5 each. V0 joins them into one timeline: `GOPRnnnn` + `GP01nnnn`… for the HERO3, and `GH01nnnn` + `GH02nnnn`… for the HERO7.
- **The camera clocks are often wrong.** Against the folder's date, the creation date is off by more than 60 days for 45 of 162 HERO7 files (they say 2016), 59 HERO3 files and 30 DJI files.
  - The time comes, in this order, from: the HERO7's GPS time (GPMF), the DJI telemetry, the folder's date, and matches with the phone photos of the same event (V8).
  - The result is one clock offset per camera per event.
- **It's a library of its own, not a copy.** Only 30 files (2.8 GiB) are byte-identical with OneDrive videos. Near copies, such as exports of these clips stored in OneDrive, are V6's job.
- **The events match the photos.** All 63 dated folders (`YYYY-MM-DD <event>`) have OneDrive photos within a day, and 35 have an OneDrive album within 3 days (15 on the same date). Clips cut from the raw footage can therefore go into the album of their event.
- The drone records no audio, so 735 GiB has no speech or sound signals. GoPro audio is mostly wind. The HERO7's `.WAV` files, where present, are the better audio track.
- No GoPro HiLight tags were set: there were 0 in the 1,052 GoPro files read.

## The idea

**A video is a timeline.**
- Frames sampled from it are photos to the existing stages, under the id `<video>@<ms>`, so DINOv2, SigLIP 2, Q-ReAlign, faces, expressions and NudeNet run unchanged.
- The audio gives speech, speakers and sound events.
- The seconds are grouped into segments, and each segment is scored.
- A photo's plan says keep or archive. A video's plan is an **edit list**: the ranges to keep, as one or more clips, plus optional colour and encode settings.
- Apply renders the edit list into new files, verifies them, uploads them, and moves the original to the archive. The original is deleted only by the final decision, as with photos.

## Stages

### V0 Probe (CPU, ~1 min)

- **ffprobe** gives each stream with its side data: the display matrix (rotation), the HDR colour tags, bit depth, and the keyframe interval from the first 30 s of packets.
- **exiftool** gives the QuickTime, Keys and UserData tags: creation date, GPS (`©xyz`), the Samsung model, capture fps (slow motion) and the Android time zone.
- The type comes from the file's magic bytes, not its extension. `**/node_modules/**` is `leave`.
- **The capture time has a recorded source**, tried in this order:
  1. QuickTime `CreateDate`. Samsung writes UTC, so the local time comes from `AndroidTimeZone`, or from the UTC offset of the photos around it;
  2. the file name;
  3. OneDrive's created time from the manifest;
  4. the folder's neighbours;
  5. a match with photos (V8).
- New table `video`: duration, fps, capture fps, codec, profile, bit depth, size, rotation, HDR, audio, GOP, bitrate, time and its source, place, device.

### V1 One read per file (decode)

One ffmpeg process per file writes everything that needs the decoded file, so the 358 GiB is read once:

| Output | Size for the library | Used by |
|---|---|---|
| Frames at **1 fps**, 512 px long side, JPEG, in `cache/…/video/<id>/` | ~200k frames, ~6 GB | every vision stage (V3) |
| Tiny grey frames at 5 fps (160 px), kept in memory | none | motion and shake (V3), cut detection (V5) |
| Audio, 16 kHz mono FLAC | ~3 GB | V2 |
| Chromaprint fingerprint, EBU R128 loudness every 100 ms | small | copies (V6), quiet/loud parts |
| A review proxy: 540p H.264 at ~1 Mb/s | ~24 GB | the review page, which can't count on playing HEVC/4K originals in every browser |

- **The cheap variant** decodes only keyframes (`-skip_frame nokey`) for the 1 fps frames, plus the audio. That is nearly free on the CPU, because phone keyframes are about 1 s apart (V0 measures it).
- The full decode (motion, proxy) runs on NVDEC once the 4090 is free.
- H.264/HEVC software decode on two cores also works, at idle priority, but takes most of a day.

### V2 Audio (CPU now, GPU later)

- **Speech map:** Silero VAD, on the CPU at minutes for the library. Only speech regions go to the ASR. That avoids Whisper's well-known Polish hallucinations on music and silence ("Napisy stworzone przez społeczność Amara.org").
- **Sound events:**
  - The model is PANNs CNN14 (AudioSet's 527 classes) or CLAP zero-shot, on the CPU.
  - Laughter, applause, singing and a baby crying count as **value**. Wind and handling noise count as **quality problems**. Music marks montages and parties.
- **Transcripts:** a bake-off on Polish home audio (see Models). Every segment keeps its words with timestamps, language, model and a confidence score. Two models that agree make a confident line.
- **Speakers:**
  - Diarization gives each video's speaker turns, as `Speaker 1`, `Speaker 2` and so on.
  - Each speaker's voice embedding is clustered across the library, like faces.
  - A voice is linked to a person when its turns overlap one visible face track whose lips move. Active speaker detection (Light-ASD, small) gives that, or failing it, consistent overlap across several videos.
  - The names then come from the People page. No new naming round is needed unless a voice never matches a face.
- **Search:** transcripts go into FTS5 and Qwen3-Embedding, like the OCR text (P24).

### V3 Frame signals (B50 + 4090 after the photo run)

- **The existing stages run on the frames:**
  - DINOv2: copies, takes and alignment;
  - SigLIP 2: tags, change points, and zero-shot "inside a pocket", "floor", "ceiling", "blurred";
  - Q-ReAlign: technical and aesthetic quality;
  - faces with expressions: smiles are value;
  - NudeNet: the ` PRIV` rule (P26) applies to clips too;
  - sharpness (Tenengrad) and exposure.
- Orientation comes from the rotation tag. The VLM's "upright?" check (P36) runs only when the tag says 0° but the frames look sideways.
- **Motion** comes from the 5 fps grey frames: global motion by phase correlation. Smooth motion is a pan. High-frequency jitter is shake. A burst is a whip pan or the camera swinging down. All of it runs on the CPU.
- About 200k frames at the photo run's measured rates (20–60 items/s a stage) take a few GPU hours.

### V4 Faces and people

- Faces are detected at 1 fps and linked into **tracks** by box overlap plus embedding.
- Each track is filled in at 5 fps over its span, to pick its best face: sharp, frontal, eyes open.
- A track's embedding joins the library's people the way a photo face does, by nearest centroid, under the P57 rules.
- Faces seen only in videos become new cards on the People page. A second naming round shows only those.

### V5 Segments

- **Hard cuts** exist only in edited files (exports, compilations, forwards): TransNetV2 on the tiny frames. Phone originals are one take and skip this.
- **Parts of a take:**
  - Change points over a per-second feature vector: the SigLIP embedding, motion, faces and who, speech, sound class, loudness.
  - The method is KTS or `ruptures` PELT, aiming at 5–20 s segments.
  - Boundaries are snapped to pauses in speech.
- **Each segment's scores:**
  - **Junk:**
    - black or covered;
    - pocket;
    - floor or sky;
    - blur;
    - shake or a whip pan;
    - nothing changes for a long time;
    - the fumbling at the start and the end.
  - **Value:**
    - named people;
    - faces and smiles;
    - speech, and who speaks;
    - laughter, singing, applause;
    - novelty against the rest of the video;
    - aesthetic score.
  - **The VLM's call** on close ones: 4 frames plus that part's transcript, asking "junk, fine or highlight?".
- The weights start hand-set. After the sample review they are fitted to the user's labels, as the burst model was (P23).

### V6 Triage: the edit list

**Whole videos:**

| Verdict | How it's found |
|---|---|
| copy | Exact copies (S1). Re-encodes, trims and WhatsApp versions: DINOv2 frames aligned in time, plus Chromaprint. The best original stays (resolution, bitrate, a real capture time), with P58's one-storage rule |
| junk | Every segment is junk, or the clip is a few seconds of nothing (accidental, pocket) |
| take | The same moment as a better video, minutes apart, like a photo burst (question 4) |
| keep / trim / split | Kept whole, as kept ranges, or as several clips |

**Inside a video**, each segment is kept, cut or left unsure, under these rules:
- **Head and tail trims** are the common case: the first and last 1–3 s pointing at the floor, or reaching for the button.
- **Speech is never cut through.** Cuts snap to pauses and word boundaries, with 0.3 s of padding.
- A cut in the middle must be at least ~4 s long, or the part is kept. Kept ranges less than 2 s apart are merged.
- **Value is protected:** named people speaking, children, singing and laughter are never cut automatically, only proposed.
- An **edited export whose parts match source clips** is linked to them. Whether the sources stay is the user's call (question 9).

### V7 Descriptions and tags

- **One Qwen3.8 call per video**, with the same JSON schema as the photo captions:
  - 8–16 frames from the kept parts, each labelled `t=12 s`;
  - the transcript with speakers, cut to ~1–2k tokens;
  - the people named in each part;
  - the place (Photon);
  - the album's context.
- The answer is a title, a description (PL + EN, as for photos), tags mapped to `vocab.yaml` and joined with SigLIP's zero-shot tags, and **one line per kept part** (chapters).
- Qwen3.8 takes video natively, and llama.cpp has native video input in `mtmd` since mid-2026 (reported by a secondary source, to be checked on Mimir's b12033). The sample compares that with frames picked by V3. The picked frames are the default, because they are sharp and labelled with their times.

### V8 With the photos: albums, splicing, frame grabs

- **Videos are items in S11**, like photos:
  - time: the clip's start;
  - place: GPS, or the photos around it;
  - people: face tracks and named voices;
  - look: the mean SigLIP embedding of the kept frames.
  
  P25 applies: a video taken during an album's event joins that album. P39 applies too: other people means another event.
- **Each clip is sorted on its own.** A long recording that spans two events goes to both.
- **Videos with no time** (991):
  - OneDrive's created time and the file name;
  - the folder's neighbours;
  - their look: DINOv2 frames against the photos of candidate albums. A video shot where photos were taken matches them strongly.
- **Splicing into albums:**
  - Kept clips sit among the album's photos by time.
  - Each clip's `CreateDate` is the clip's start, so OneDrive Photos, Immich and Windows sort it among the photos.
  - Each is named `YYYYMMDD_HHMMSS` from its start, like the phone's files.
- **Frame grabs** (question 6): a moment that exists only on video, with no photo within a few minutes, can give its best frame or two as a JPEG in the album:
  - only from sources of 1080p or more, sharp, with good faces;
  - with the EXIF time set to the frame's time;
  - with XMP naming the source video.
- Photos and videos of one moment are shown together in the review. Nothing is archived across media automatically.
- **Motion Photos** keep their embedded clip. Stripping it, or saving a clip worth keeping as a video, is a later, opt-in option (S12).
- **The order:** the video pass runs on the `onedrive` session **after the photo review is confirmed**. The albums are then fixed and the people named. Videos join the reviewed albums, and the rest make their own events and folders, like the photos.

### V9 Render: cut, colour, encode, check

**Cutting:**
- When a file isn't re-encoded, trims and splits are **lossless**: `-c copy` at keyframes. That means up to ~1 s early or late at phone GOPs, and never into speech.
- When it is re-encoded, the cuts are exact, and the colour and the encode happen in the same pass.

**Encoding:**
- The bake-off on the sample compares:
  - AV1 on the 4090's NVENC;
  - AV1 on the B50's QSV;
  - SVT-AV1 on the CPU (the best per bit, slow);
  - HEVC NVENC as the fallback.
- `ab-av1 crf-search --min-vmaf` picks each file's quality setting from samples.
- A file is left alone when it would shrink by less than ~25%: most HEVC from the newest phone, the AV1 files and the low-bitrate WhatsApp clips.
- HLG stays 10-bit HDR.
- Audio is copied, not re-encoded.

**Metadata:**
- `CreateDate` is moved to the clip's start.
- GPS, the device, capture fps, rotation and HDR tags are carried over.
- The title, description, keywords and people are written as XMP. The file is new anyway, so this costs no extra upload (S12).

**Checks per clip:**
- the duration and frame count match the edit list;
- **VMAF** against the same range of the original: e.g. mean ≥ 95 and the 1% low ≥ 90;
- the audio is in sync (Chromaprint offset);
- the file decodes end to end;
- the file is smaller than the original's range.

A clip that fails stays the lossless cut.

**Playback** is checked by the user on the sample before anything runs at scale: OneDrive web, the OneDrive and Samsung Gallery apps on the phone, and Windows Photos. OneDrive's own preview of AV1 is the main unknown, and the codec choice (question 5) waits for it.

### V9b Colour, for camera originals and raw footage

This revises design.md's "out of scope: editing pixels" for videos only (decision PV3).

- **Correction by default, not grading:**
  - Exposure, white balance and contrast are evened out per shot.
  - One correction is held for the whole shot, so nothing flickers.
  - It is computed from the shot's frames and applied as a 3D LUT (`.cube`) during the encode (ffmpeg `lut3d`, or libplacebo on a GPU).
  - Image-adaptive 3D-LUT models (Zeng et al. 2020, AdaInt 2022) are tiny and trained on retouched photos. Run on a shot's keyframes and averaged, they give one LUT per shot. A percentile stretch with grey-world white balance is the baseline to beat.
- **Log footage** gets the maker's LUT first (D-Log M, GoPro Log/Flat, V-Log, S-Log3, Samsung Log → Rec.709), then the correction. None was found so far, but newer drones and GoPros record it.
- **Cameras are matched within an event.** Drone, GoPro and phone clips of the same day are corrected towards one reference, so the album doesn't jump between looks. The phone's clips are the default reference.
- **HLG** stays HDR in the library copy. An SDR version is made only for exports and montages (libplacebo, BT.2390).
- **Stabilisation is optional**, for very shaky originals. vid.stab works in two passes on the CPU and crops ~5–10%. Gyroflow needs gyro data that only action cams, drones and some cameras record; these phones don't.
- **The review** shows the before and after side by side on the proxy. The user approves per album or per clip.

### V10 Apply on OneDrive

- A video's result is **new files**, so `mimir-onedrive` needs an **upload** op: rclone `copyto`, then a hash check against the plan. Photos only needed moves.
- The original moves to `Archive/OrderMyPhotos/` on the server side, and is deleted only by the final decision.
- **Upload volume:** the kept, re-encoded videos come to roughly 100–180 GiB (see Space). The uplink speed is measured on the sample.
- Videos leaving `Samsung Gallery` follow P30 and the two-way sync test, like photos.
- **Raw storages** get their own apply, separate from OneDrive's.
  - The steps: write the raw masters next to the originals, check them, then move the originals and junk files to the share's `_archive/`. Deletes follow the final decision.
  - It runs with the same ops log, dry run and undo, through the only read-write mount of the share.
- Waves:
  1. exact copies (already wave 1);
  2. near copies;
  3. junk videos;
  4. trims and splits;
  5. re-encodes;
  6. deletes after the cooling-off period.

## Raw footage: drones and action cams

The same stages apply, with these differences.

**The storage:**
- Each raw storage is a **library of its own**: a session with its own DB, its rows in the catalog (P58), and its share mounted read-only on Mimir (plan.md Phase 5b). For `apollo` that's SMB or NFS, mounted `ro`, and logged in the mimir changelog.
- Reading 1.9 TB over 1 GbE takes ~5 h, so V1 reads each file once and keeps the frames, audio and a 540p proxy (~40 GB for 95 h) in the cache.
- Mounting and reading wait until the photo run is done.

**V0 for raw footage:**
- GoPro chapters are joined into one recording.
- The telemetry is read: GoPro's GPMF (gyro, accelerometer, GPS with UTC time, settings) and DJI's `.SRT` (altitude, GPS, ISO, shutter, colour mode).
- Each camera's clock offset per event comes from GPS time, the folder's date and photo matches.
- The HERO7's `.WAV` replaces the MP4's audio when present.

**V5 signals that only raw footage has:**
- **Drone:**
  - Junk: takeoff and landing (the altitude, or vertical motion with the ground in view), gimbal resets, the compass dance, propellers in the frame, hovering where nothing changes, and test flights.
  - Value: smooth reveals, orbits and slow pans. For a drone, smooth motion is good, unlike handheld.
- **Action cams:**
  - From telemetry where there is any (HERO7): speed, g-force, jumps (free fall in the accelerometer), falls and stops.
  - Junk: lift rides and walking (SigLIP plus low speed), the camera in a bag (dark), a fogged or wet lens, and a mount pointing at the sky.
  - Value: the people from the People page, speech and laughter despite the wind, scenic stops, and peaks of speed.
- **Repetition:**
  - Ten similar runs keep the best one or two. The choice uses novelty against the rest of the event, plus SigLIP diversity (MMR).
  - The same moment filmed by the drone, a GoPro and a phone is one "scene" with several takes.

**Selection by budget:**
- Raw footage is mostly long, uneventful takes. So each event gets a **highlight budget**, e.g. ≤ 10% of the raw length, or N minutes per hour (question 10).
- The best-scored, varied segments fill the budget, and the review shows the rest as cut.

**Colour and picture:**
- Per-shot correction, plus matching the cameras of the event (V9b).
- Optionally, GoPro's fisheye corrected to a linear look with Gyroflow's lens profiles or ffmpeg `lenscorrection` (question 11).
- vid.stab for shaky HERO3 parts, which have no gyro and no stabilisation. The HERO7 already has HyperSmooth, and the drone has its gimbal.

**Where results go:**
- The kept clips, cut, corrected and encoded, go to the **OneDrive album of their event**, or a new album for it, and sort among its photos by their corrected time (V8).
- **The raw storage is culled too** (user, 2026-11-06; question 1 answered).

**Culling the raw on the NAS:**
- **What stays:**
  - For each highlight, a **raw master** of its range stays in its event folder. It is a lossless cut (`-c copy`), at the camera's own quality.
  - It is widened to the keyframes before and after, plus ~2 s of margin on each side, so a later re-edit has room.
  - The graded clip in OneDrive and its raw master are the same moment in two storages on purpose. That is the "unless specified" of P58's one-storage rule.
- **What goes:** whole raw files with nothing kept, and every original that was cut. Both are moved to an archive folder on the same share (`RAW/_archive/OrderMyPhotos/<event>/`, the same layout as OneDrive's `Archive/OrderMyPhotos/`). A move on the same share takes no space and is one rename to undo.
- **Deletes:** only by the final decision, after the cooling-off period, as for photos. The share's recycle bin (`#recycle`) is on, so a delete stays recoverable until it is emptied.
- **What a cut carries over:**
  - **Telemetry:** the GPMF track. ffmpeg drops or mangles it unless it's mapped explicitly, so `gpmf-parser` checks it on every cut.
  - **Sidecars:** the DJI `.SRT` with its times shifted, and the HERO7 `.WAV` cut at the same range. `.LRV` and `.THM` go with their original to the archive.
  - **Metadata:** `creation_time` is moved to the start of the range. The clock correction is kept in the DB and written into the OneDrive clip, not into the raw file.
- **Names:**
  - `<original stem>_<start>-<end>.MP4`, with the start and end in seconds, so every master points back to its original.
  - A chaptered recording is cut on the joined timeline, so a range that crosses a chapter becomes one file.
- **Checks before the original moves:**
  - the master decodes end to end;
  - its duration matches the range;
  - its frame hash matches the same frames of the original;
  - its telemetry and sidecars are present.
- **Writes are a separate step.** The share is mounted read-only for the whole analysis. Only the apply step writes (V10), through its own read-write credential:
  - a Synology snapshot of the volume first, if the volume supports it (to check);
  - hash-checked ops, an op log, a dry run and undo, like `mimir-onedrive apply` (P56).
- **The review per event** shows two things: the highlights that go to OneDrive, and the raw cull. The cull is what stays on the NAS and what goes to the archive, with its size.

## Models

| Need | First choice | To compare | Runs on |
|---|---|---|---|
| Speech regions | Silero VAD | — | CPU |
| Sound events | PANNs CNN14 (AudioSet) | CLAP zero-shot, BEATs | CPU |
| Polish ASR | **Whisper large-v3 / large-v3-turbo** (faster-whisper) and **Parakeet-TDT-0.6B-v3** | Canary-1B-v2; Qwen3-ASR-1.7B (52 languages; its forced aligner has no Polish); VibeVoice-ASR (ASR, speakers and timestamps in one pass, "50+ languages", Polish to check) | 4090 for bulk; CPU for the bake-off |
| Speakers | **pyannote `speaker-diarization-community-1`** (pyannote.audio 4, CC-BY-4.0) | DiariZen; VibeVoice-ASR's built-in speakers | 4090 / CPU |
| Voice ↔ face | Light-ASD (active speaker detection) | overlap statistics alone | 4090 / CPU |
| Hard cuts | TransNetV2 | ffmpeg `scdet` | GPU / CPU |
| Frame signals, faces, nudity | the photo stages (models.md) | — | B50 + 4090 |
| Clip quality | frame signals plus motion | DOVER (technical + aesthetic VQA), only if the frame signals miss | 4090 |
| Descriptions, segment calls | **Qwen3.8-27B**, with our frames | llama.cpp native video input; Qwen3-VL / Molmo2 are reference points for temporal grounding | 4090 |
| Encode | AV1 NVENC, AV1 QSV, SVT-AV1, HEVC NVENC | — | 4090 / B50 / CPU |
| Quality gate | VMAF (libvmaf, `vmaf_cuda`) | — | 4090 / CPU |
| Colour | image-adaptive 3D LUT per shot, matched across the event's cameras | percentile + grey-world baseline | CPU / GPU |
| Telemetry | GoPro's `gpmf-parser`; a small parser for DJI's `.SRT` | — | CPU |
| Lens and stabilisation | Gyroflow lens profiles; vid.stab | ffmpeg `lenscorrection` / `v360` | CPU / GPU |

On Polish, the only published comparison found is NVIDIA's MLS table (arXiv 2509.14128): word error rates of **Parakeet-TDT-0.6B-v3 7.3%**, Canary-1B-v2 8.8% and Whisper large-v3 8.9%. That is read speech. Home video is far-field, noisy, with children and music, so the library's own sample decides.

Licences are to be checked against the Hugging Face API before use, as in models.md. Non-commercial ones are acceptable for the private library.

## Data model (additions)

- **`video`** (V0): one row per distinct video.
- **`frame`**: `fid`, `video`, `t_ms`, `kind` (sample, track, best, grab) and `path`. Signals, faces and expressions key on `<video>@<t_ms>`, so those tables don't change; `face` gets an optional `track`.
- **`track`**: video, person, t0, t1, best frame, embedding.
- **`speech`**: video, t0, t1, speaker, text, words (JSON), language, model, confidence. **`speaker`**: video, label, embedding, voice cluster, person.
- **`sound`**: video, t0, t1, label, probability.
- **`segment`**: video, t0, t1, kind (shot or part), scores (JSON), verdict, why, source (auto or user).
- **`clip`**: video, n, ranges (JSON), start time, destination name, LUT, encode settings, VMAF, size, state. The edit list is these rows.
- User changes to segments and clips are labels. They train the weights (V5).

## Review

Videos appear in their album's review unit (P47), next to the photos:
- **A card** shows a poster frame, the duration with the kept duration, and the verdict.
- **The timeline** under it shows:
  - thumbnails per second, with kept ranges plain and cut ranges striped with their reason;
  - transcript lines under the thumbnails, with speakers;
  - markers where named people appear.
- **Play** the proposal (the proxy, with the cut ranges skipped) or the original.
- **Actions:**
  - keep all;
  - accept trims;
  - drag a range's edges;
  - split here;
  - archive the video;
  - "a take of that one";
  - colour before and after.
- The People page gets the new face cards from videos and, optionally, voices that never matched a face.

The review page shows the user's videos. Claude checks it with API and text tools only, never screenshots (as for photos).

## Resources: what can run now, and what waits

The photo run owns both GPUs:
- the 4090 holds Qwen3.8 with 4 slots (24.1 of 24.5 GB);
- the B50 peaks at 15.7 of 16 GB during the vision phase (mimir changelog, 2026-11-06).

So neither GPU can take a decoder or a model without risking the run. The B50's media engine is idle, but its VRAM isn't free.

The host CPU is ~12% busy (load ~2.5 of 24 threads).

| Work | Needs | During the photo run? |
|---|---|---|
| V0 probe | 1 core, ~1 min | **yes** (the exiftool half ran on 2026-11-06) |
| V1 cheap variant: keyframe frames, audio, Chromaprint, loudness | 2 cores at idle priority, idle I/O, ffmpeg + fpcalc in the image (`omp` 0.3) | **yes**, a few hours |
| V2 VAD and sound events | CPU, minutes | **yes** |
| ASR and speaker bake-off on the sample (~40 clips) | 4 CPU threads, ~1 h, idle priority | **yes**, if wanted |
| V1 full decode (motion, proxies) | NVDEC, or 2 cores for most of a day | after |
| V3–V7 at scale | 4090 + B50 | after |
| V9 render and VMAF | NVENC / QSV / CPU | after |

**After the photo run, rough estimates** to be replaced by measurements:
- full decode: 2–3 h on NVDEC;
- frame stages: 2–4 h;
- ASR and speakers on the 4090: 1–2 h;
- descriptions: ~2–3 h (4 slots);
- segment calls: 1–3 h;
- hardware encode with VMAF: 4–8 h. SVT-AV1 instead would take days of CPU.

In all, about **1–2 days of machine time**, plus the review.

The NAS's `RAW` folder adds ~1.8× the hours: about 2–3 more days, plus ~5 h to read it over the network.

## Space

| Step | Rough size afterwards |
|---|---|
| Now | 358 GiB |
| Exact copies (wave 1) | ~311 GiB |
| Junk videos and trims (10–25% of the length, a guess) | ~240–280 GiB |
| AV1 at ~35–50% of H.264's size and ~70% of HEVC's | **~110–180 GiB** |

**The NAS's `RAW` folder:**

| Step | Rough size |
|---|---|
| The raw files | 1,868 GiB |
| Highlights kept (10–25%, the budget) | 190–470 GiB of raw |
| Encoded: AV1 at ~8–10 Mb/s for 1080p60 instead of ~30, and ~20–25 Mb/s for drone 4K instead of ~100 | **~50–150 GiB** into the OneDrive albums |
| The raw masters left on the NAS (the kept ranges with their margins) | **~200–500 GiB** of the 1,868 |
| The NAS's archive until the final decision's deletes | the rest, ~1.4–1.7 TB, freed after the cooling-off period |

The NAS has 6.4 TB free (2026-11-06), so the masters can be written before any original moves.

The sample replaces every guess here before the space report (plan.md Phase 7) shows it.

## The sample and how it's judged

**~40 clips, picked from the DB by type:**
- 10 phone 1080p H.264: day, night, indoors with children, talking;
- 5 HEVC from the newest phone, 3 of them HLG;
- 5 Panasonic 4K;
- 5 MJPEG AVI;
- 5 WhatsApp;
- 3 over 10 min;
- 4 edited exports;
- 3 obvious junk;
- from the NAS: 5 drone clips, 5 HERO3 (one of them a chaptered recording), 4 HERO7 with telemetry, and 2 Garmin. They are picked from one or two events that also have phone photos and clips, so the matching of cameras and clocks is tested too.

It is a test session in `cache/photos/tests/` like s5–s8, reflinked, and it never touches OneDrive (P48).

**The user:**
- labels the segments in the review;
- corrects the transcripts of ~10 Polish clips;
- names the speakers;
- checks playback of the encoded files on their devices.

**Metrics:**

| What | Metric |
|---|---|
| Cuts (the safety metric) | the **false-cut rate**: kept-worthy seconds proposed for cutting, target ~0. Also the precision of junk videos, and head and tail trims within 1 s of the user's |
| Transcripts | WER/CER on the corrected clips, made-up lines on clips without speech, and DER for speakers |
| Faces | track-to-person accuracy against the People page |
| Encoding | size, VMAF, speed per encoder, and the playback grid |
| Colour | the user's approval rate on before/after pairs |

## Order of work

1. **Now, beside the photo run:**
   - `omp` image 0.3 with ffmpeg, fpcalc and Silero;
   - V0 into the DB;
   - the policy fixes (`node_modules`, sniffing, `Video/Private`);
   - V1's cheap variant and V2's VAD and sound events, at idle priority;
   - the sample picked;
   - optionally, the ASR bake-off on the CPU.
2. **After the photo review:**
   - the sample session end to end (V3–V9);
   - the user's labels and playback checks;
   - the codec choice.
3. **The library:**
   - V1 full decode and V3–V8 on the `onedrive` session;
   - the second naming round;
   - the video review per album;
   - render and apply in waves (V10).
4. **The raw storages, one library at a time:**
   - the NAS share mounted read-only on Mimir;
   - V0 with chapters, telemetry and clocks;
   - V1 once over the network;
   - V3–V8, with the highlight budget;
   - the review per event;
   - clips into the OneDrive albums;
   - the raw cull on the NAS: masters written and checked, originals and junk to `_archive/`, deletes after the cooling-off period.

## Decisions to take (proposed)

- **PV1 Frames are photos.** Video frames are items for the existing stages (`<video>@<ms>`). There is no second vision code path, and the speed work (P49–P62) carries over.
- **PV2 A video's plan is an edit list.** Nothing is cut until the final decision. The original goes to the archive and is deleted only by the final decision.
- **PV3 Pixels may change, for videos only:** trims, colour correction of camera originals and raw footage, and re-encodes. Each is shown before and after, and each is opt-in per run. This revises design.md's "out of scope".
- **PV4 Videos wait for the photo run's GPUs.** Only the CPU-only cheap pass (V0, V1 cheap, V2 VAD) may run beside it, at idle priority.
- **PV5 Videos sort into the reviewed albums.** The video pass is a second pass of the same session after the photo review. Videos join the confirmed albums or make their own events.
- **PV6 Raw storages are libraries of their own.**
  - The NAS `RAW` folder is the first. Each is mounted read-only, with its own session, and its rows in the catalog.
  - Its highlights are cut, corrected, encoded and placed in the OneDrive albums of their events.
  - **The raw is culled in place** (user, 2026-11-06):
    - a lossless raw master of each highlight's range, with margins, stays in its event folder;
    - cut originals and junk files go to the share's `_archive/`;
    - deletes happen only by the final decision.
  - Only the apply step writes to the share.

## Questions for the user

1. ~~The raw files after the selection.~~ **Answered (user, 2026-11-06): cull them on the NAS too.** Raw means the drone and action-cam storages, such as `apollo`'s `RAW` folder. See "Culling the raw on the NAS".
   - Still open: which other raw storages are there, and where? V0 surveys each one first.
   - Still open: is the ~2 s margin around a raw master right?
2. **Trimmed videos:** the trimmed file replaces the original in the album, and the original is archived, then deleted by the final decision, like photos. Is that right? Or should originals of trimmed videos be kept forever?
3. **Long recordings:** split into several clips, each at its own place in the album's timeline, or one trimmed file?
4. **Takes:** several videos of the same moment. Keep only the best, like a burst, or keep all?
5. **Codec:** AV1 (smaller) or HEVC (plays everywhere). Decided by the playback check on your devices unless you already know.
6. **Frame grabs** into albums for moments that only exist on video: yes or no?
7. **`Video/Private`:** leave it, like `Pictures/Private`?
8. **Transcripts:** only in the DB, search and the description, or also as `.srt` files next to the videos?
9. **Edited exports and their source clips:** keep both, or only the export?
10. **The highlight budget for raw footage:** about 10% of the raw length, N minutes per hour of recording, or no fixed budget, with only the score deciding?
11. **GoPro fisheye:** keep it as recorded, or correct it to a linear look in the highlights?

## Sources

- Canary-1B-v2 and Parakeet-TDT-0.6B-v3, with the per-language MLS table: [arXiv 2509.14128](https://arxiv.org/pdf/2509.14128)
- Polish ASR case study: [arXiv 2603.02246](https://arxiv.org/pdf/2603.02246)
- pyannote Community-1 among diarization models: [pyannote.ai blog](https://www.pyannote.ai/blog/top-7-speaker-diarization-apis-and-models-in-2026). DiariZen: [arXiv 2604.21507](https://arxiv.org/pdf/2604.21507)
- VibeVoice-ASR: [technical report, arXiv 2601.18184](https://api.emergentmind.com/papers/2601.18184), [Microsoft's post](https://techcommunity.microsoft.com/t5/microsoft-foundry-blog/introducing-vibevoice-asr-longform-structured-speech-recognition/ba-p/4501276)
- Qwen3-ASR and its forced aligner: [github.com/QwenLM/Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)
- Qwen3-VL (text timestamps, temporal grounding): [arXiv 2511.21631](https://arxiv.org/pdf/2511.21631). Molmo2: [arXiv 2601.10611](https://www.alphaxiv.org/abs/2601.10611)
- llama.cpp multimodal and video input: [docs/multimodal.md](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md), [a write-up of the video PR](https://site-one-liart-13.vercel.app/en/dgx/llama-cpp-native-video-input-ffmpeg-mtmd) (secondary, to verify)
- Video quality models (DOVER, FAST-VQA, Q-Align): [github.com/vqassessment/DOVER](https://github.com/vqassessment/DOVER), [arXiv 2505.22543](https://arxiv.org/pdf/2505.22543)
