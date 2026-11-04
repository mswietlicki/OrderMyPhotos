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

## P8 — Photos from two-way sync folders are copied, not moved, until tested (2026-11-02; superseded by P30)

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

## P25 — Unsorted photos: the album whose event was going on wins (2026-11-03, user)

The user's rule: "it's very rare that a photo taken during the time of an event or trip does not belong to that event/trip, no matter the directory it's in, so it should be moved to join the album." So S11 decides per photo, in this order:
1. **Copy:** the same bytes are already in an album anywhere in the library, so this copy is archived.
2. **Taken during an album:** the photo's time falls within an album's photo span, with a 2-hour margin. It joins that album, and its text pages go to the album's `Dokumenty/`. If two albums overlap, the one with a photo closest in time wins.
3. **Reference photos** outside any album go to their category album.
4. **Everything else** is split into events: a gap of 3 h or more starts a new one, and days away from home merge into a trip. An event with ≥ 8 kept photos becomes a new album named by the local LLM, with 60 of the user's own album names as style examples. Smaller events stay where they are.

The albums come from the **OneDrive manifest** (names, content hashes, capture times from file names), so step 1 and step 2 work against the whole library, also for albums the sample doesn't contain. The rule covers every unsorted folder (camera folders, `Pictures/Photos`, WhatsApp, downloads), but not the curated thematic folders (`Pictures/People`, `Places`, `Info`, `Other`), whose placements are deliberate.

## P26 — Full nudity goes to the album's " PRIV" sibling (2026-11-03, user)

The user wants photos with nudity marked as "Nudity" and placed in a separate, attached private album: "I care only about full nudity, partial nudity is fine for all albums", "do not add any blur, just move to the PRIV album". The library already has the convention, sibling albums like `<date> <name>` and `<date> <name> PRIV`. So:
- A local **NudeNet** detector (v3.4.2, 320n ONNX, CPU) finds body parts as exposed or covered. **Full nudity** means exposed genitals or anus, or bare buttocks and bare breasts together. Topless, shirtless, swimwear or underwear alone count as partial and are left alone. A general NSFW classifier was considered and dropped, because it can't tell full from partial nudity.
- A flagged photo in an album is proposed into `<album> PRIV`; one that's already in a PRIV album isn't. A flagged unsorted photo goes to the PRIV sibling of the album it joins, or of the new album.
- Flagged photos get the "Nudity" keyword but no VLM caption, so no description of them ends up in file metadata. They aren't blurred in the review.
- NudeNet's licence is listed as MIT on PyPI and AGPL-3.0 on GitHub. Either is fine for private use on Mimir, where nothing is distributed.

## P27 — People are named from the user's People folders and album names (2026-11-03, user)

The user: "I often name albums by a place and the people that were there. For this to work you will need to have face identification working and trained." So S8 comes before album naming, and it trains on what the user already did by hand:
- **`Pictures/People/<Name>` folders** (16, ~830 photos) get a new folder policy, `people`. Their faces are learned, and the photos are left as they are: no culling, no proposals, no review page each. A face cluster that's the dominant face in a person's folder (in ≥ 30 % of its photos with faces, at least 3) is that person.
- **Album names:** the local LLM reads every album name once (60 per call) for the people it mentions, in their base form ("z Zosią" → Zosia, "Urodziny Jacka" → Jacek). It matches them to the People folders, diminutives included. A still-unnamed cluster that recurs in at least two albums naming one person gets that name.
- **The owner** is the OneDrive account's display name, from the manifest's metadata. Their cluster is marked, so album names don't say "z <owner>", and the name isn't kept in the repo.

Clusters come from average-linkage agglomeration of the InsightFace `buffalo_l` embeddings at cosine distance 0.55. Only faces ≥ 4 % of the frame height with detector score ≥ 0.65 are clustered, and a cluster needs 3 faces. The names are proposals for the People view and, later, Immich. The album namer gets "People recognized: Zosia (12 photos), …" and is told the owner's style: a place or activity plus "z <name>".

The namer is measured on the user's own albums: for unsorted photos that join an existing album, it proposes a name as if the album were new, and that's printed next to the user's.

## P28 — OCR keeps its structure; an LLM repairs artifacts behind a guard (2026-11-03, user)

The user found the text files poor: "Unlimited-OCR-2 should be able to generate more structured text with location and descriptions", then "run the text with an LLM for fixing mistakes … normal OCR artifacts. If Qwen3.8 is bad with Polish we should use a different model or even OpenRouter", and asked me to read the OCR and Qwen's output myself.
- **Structure:** the model returns typed layout blocks with a position on a 0–999 grid (`<BLOCK>type<LEVELn>[x0, y0, x1, y1]<FORK>text`). The first version threw them away. Now each page keeps its blocks, with type, level, box and text, in the DB and in `<name>.ocr.json` in the bundle. The `.txt` is Markdown: headings by level, paragraphs, tables, figures and captions, with the running head and page number on one line at the top.
- **What I read:** on the guidebook pages most of the text is right. The errors are OCR artifacts: wrong diacritics (ś read as č, ł as t, missing ą/ę), words split at line ends, and a few misread words.
- **Qwen3.8 as the repairer:** about 70 % of its changes were right. About 25 % were wrong, some changing the meaning: "zlebu" → "zlewu" (żlebu), "Myna" → "Młynem" (Mylną), "Przętecz" → "Przełącz" (Przełęcz), "połoga" → "połażą", a non-word "Kościeliec", and the foreign-language names of the peak turned into Polish forms. A free rewrite isn't safe.
- **The guard:** a change is kept only if the word keeps its letters and length and differs by diacritics or the OCR's typical confusions (ł/t, ś/č, …), or if it joins a word split at a line end. Everything else keeps the OCR's version, and both the kept and the dropped changes are recorded. On the test sentence it kept every right fix and dropped every wrong one. It also drops a few right fixes that change letters ("praz" → "przez", "Kiemensiewicz" → "Klemensiewicz"): precision first.
- **Not used yet:** Bielik (a Polish LLM, Apache-2.0), which may fix more within the guard, and OpenRouter. If OpenRouter is ever used, it gets its own decision record and only printed public text (books, guides, signs), never receipts, documents or anything personal.
- **Text pages only:** a photo is OCR-read only if the VLM first says it shows text worth reading. Without that check, photos without text were "turned", because "is the text upright?" got a no.

## P29 — No photo stays unsorted: what makes no album goes to a thematic folder (2026-11-03, user)

The user: "do not leave photos in the incoming directory, all photos need to be moved somewhere. If there is no place for them in any album then add them to one of the special directories like documents, food, people, home, vistas", and "in the end of the whole OneDrive processing no photo should stay unsorted". So P25's step 4 no longer leaves small events in place. Each of their photos goes, in this order, to:
1. **A person's folder**, when a named person (P27) is the main subject (their face ≥ 12 % of the frame height). That's their undated album if they have one with ≥ 50 photos, otherwise `Pictures/People/<name>`.
2. **The thematic folder it looks most like.** Each of the user's own folders (`Info/*`, `Other/*`, `Places/*`, undated `Albums/*`) is the average SigLIP 2 embedding of its photos. The best folder must reach a cosine of 0.55 and beat the second by 0.02. `Albums/Facebook` and `Albums/Screeshots` say where photos came from, not what they show, so they're not themes.
3. **`Other/Widoki`** for a landscape or city view, **`Other/Dom`** within 2 km of home, else **`Other/Random`**.

In the review a proposed album has a second answer, "Not an album: thematic folders", which routes its photos the same way.

## P30 — Album folders in the wrong place move to `Albums/<year>/`; the phone gallery is moved too (2026-11-03, user)

The user: "if there are albums in different places than the default one, those should be moved", and on Samsung Gallery: "no, move it. Let's clean the phone copy then". So:
- Every folder named like an album (`YYYY-MM-DD name`, `YYYY-MM name`) outside `Pictures/Albums/<year>/`, or under the wrong year, is proposed to move there. If an album of that name is already there, the two merge. The list is on the bundle's index page.
- **What isn't an album folder:**
  - a day folder named by its date alone (`Camera Uploads/2013-10-19`);
  - a device's day folders, where the same name follows the date in three or more sibling folders;
  - a dated folder inside an album, or inside `Pictures/Private`, `Other`, `Info`, `Places` or `People`.

  Their photos are sorted photo by photo like any unsorted ones. On the library's manifest the first version found 452 folders, 391 of them camera-upload days; with these exclusions it finds 36: phone-gallery albums, older named event folders and a few named upload folders.
- Photos from Samsung Gallery's synced folders are moved like any others, and the phone's copy is cleaned. This supersedes P8. The sync test from plan.md still runs first, but it now checks that the clean-up doesn't remove anything else, not whether to copy.
- Every move, merge and delete happens only in the production run on OneDrive, after review. The sample runs never move anything (user: "of course all of this will be done when we run the production run on OneDrive, not during testing").

## P31 — Downloaded images go to media folders, never to an event album (2026-11-03, user)

The user: "internet download images and photos like memes, wallpapers, concepts etc. … should also be moved to thematic albums but not to Event/Trip albums, even if added during the trip". So, before P25's "taken during an album" step:
- **What counts as downloaded:** the VLM caption says `media`. Or, for a photo without camera EXIF, SigLIP 2's zero-shot kind says media. The kinds now include wallpaper, digital art/illustration/concept art and infographic, next to meme and screenshot. Without a caption, a download-style name also counts (`Screenshot…`, `wallhaven-…`, `unnamed…`, `download…`). A camera photo is media only when both the caption and the kind say so.
- **Where it goes:** the user's media folder it looks most like (`Other/Meme`, `Funny`, `Art`, `Ideas`, `Wallpapers`, `Info/Infographic`). Otherwise the folder for its kind: `Other/Meme`, `Other/Wallpapers` (new), `Other/Art`, `Other/Infographic`, `Albums/Screeshots`. Otherwise `Other/Random`.
- The zero-shot kinds are recomputed from the stored SigLIP 2 embeddings when the label list changes (`+zeroshot@2`), with no new image pass.

## P32 — Messenger photos are decided one by one (2026-11-03, user)

The user: "WhatsApp (messenger) photos are harder because those could be event photos, documents, memes or other. You will have to decide." Photos from WhatsApp, Messenger, Facebook, Signal and Telegram have their EXIF stripped and only carry the day they arrived. Here's how they're decided:
- **What the user did by hand:** of the 53 WhatsApp photos in their dated albums, 43 arrived during the event, 1 three days after, 3 about 40 days after and 6 before.
- **How similar is a photo to its own album** (SigLIP 2, best cosine to the album's photos taken more than 30 min apart, 7 sample albums): own album median 0.82, 10th percentile 0.70; best other album median 0.72, 95th percentile 0.78.
- **The rule:**
  - A media image goes to a media folder (P31).
  - **The arrival day alone says little.** Across the library, 63 % of the 3,717 WhatsApp images arrived during some album's days (or up to 3 days after), and the user put 1.9 % of those into that album. A first version that let the day decide whenever the album had nothing to compare with joined 91 of the sample's 250 images to albums.
  - A real photo joins an album if it arrived during the album's days, or up to 3 days after, and looks like the album's photos (best cosine ≥ 0.80, around the own-album median and above the other albums' 95th percentile).
  - Otherwise it joins an album within 60 days that it looks very much like (≥ 0.88), which covers the photos sent weeks later.
  - An album with no processed photos takes no messenger photos. Albums are processed before the unsorted folders, so in the library run this only matters for the sample.
  - A document goes to its category folder, not the album: it was sent by someone, and the day alone doesn't tie it to the trip.
  - Everything else goes to a thematic folder (P29), often a person's folder. Messenger photos never make an album of their own, because without a time of day they can't be placed in an event.
- The thresholds come from 7 albums. With the whole library there are more albums to look like, so they're checked again on the first OneDrive batch.

## P33 — Photos that join an existing album are processed and reviewed with it (2026-11-03, user)

The user: "unsorted photos should also be added to already existing processed albums", and "it's possible that sorting photos will create new bursts to process". So a photo that joins an album (P25, P32) isn't a side proposal:
- **Placed before grouping.** A new stage, `place`, runs before S4. It makes the S11 decisions that need no captions or LLM: copies, downloads, messenger photos, and photos taken during an album.
  - A photo with a real capture time is grouped with the album's photos at that time. A phone shot and a camera shot of one moment become one burst, and the best of them is kept.
  - A messenger photo only has its arrival day, so it's placed at the moment of the album photo it looks most like. A re-sent copy of an album photo then shows up as a copy, and a friend's shot of the same moment joins that burst.
- **Captioned with the album's context:** its album name and notes, not its folder's.
- **Reviewed on the album's page:**
  - The photo shows next to the album's own photos, with a "+ <folder>" badge, and the index counts it as "joining".
  - A burst with joining photos is marked, because a reviewed album needs that burst reviewed again: a joining photo may now be the best frame.
  - `o` keeps a joining photo out of the album, and it then goes to its thematic folder. `o` works the same on an album's own photos (user, 2026-11-03: "how do I remove photos from the album?"). The label names the album it was taken out of, and S11 routes the photo to the thematic folder it fits. If that's the album it left, it goes to `Other/Random`. To get rid of a photo altogether, archive it (`a`).
  - Labels name a joining photo by its path, since it lives in another folder.
- A photo that joins an album without a page in this library (only in the OneDrive manifest, not processed yet) keeps its own "Add to …" proposal until that album is processed.

## P34 — Text pages are only tagged "OCR needed"; the OCR is a post-process (2026-11-03, user)

On the s4 sample, 136 photos were OCR candidates (SigLIP 2's kind is document, label, screen, whiteboard or receipt, or the caption's category is). At 30–90 s per page on the 4090, with Qwen3.8 evicted while it runs, that's 1–2 hours. Of the 78 read before, 56 were pages of text; the rest were labels, boards and screens with a few words. The user: "If it takes so long we may run it as a post-process after all albums are finished and all photos sorted", and "for now just add a tag marker that this is all text and OCR is needed". So:
- **The `text` stage** asks the resident chat VLM three cheap questions, about 1 s each: is there text, is it mostly a page of text, and which way up is it. A page of text gets the "OCR needed" tag (`Status/OCR needed`, shown on the review page). It also gets everything else that P24 gives a text page: the `Dokumenty/` subfolder, the rotation, and a document set. Measured against the earlier OCR (300+ characters = a page), the check found 46 of 56 pages. Most of the misses are short, at 300–400 characters, and it called 7 other photos pages, 4 of them with 180–290 characters.
- **Until a page is read,** two text pages that look alike count as different pages, so all of them are kept. Retakes of the same page are found once the OCR has read them.
- **The OCR, `omp ocr`,** is a post-process and not part of `all`. It reads only the kept pages (the reviewer's verdict wins) with the block layout, then runs the guarded repair (P28). It runs when the 4090 can be lent. Its `.txt` and `.ocr.json` then appear in the bundle with the next `omp report`.

## P35 — Sorting by sessions and events, decided with album profiles (2026-11-03, user)

The user reviewed s4's sorting: "Mostly I'm NOT happy with the sort." Their labels and notes showed one cause behind most of it:
- **Photos were routed one by one.** A work presentation, five photos in four minutes, went to four different folders. A billiards evening with colleagues went to an acrobatics folder. Smartwatch internals went to `Random`, and a receipt to `Documents`.
- **Events under 8 kept photos never became albums.** All 8 photos sent to `Dom` and all 6 sent to the cat's album were taken out.
- **The pantry series:** 42 photos of jars in a shop, 45 minutes before a trip album, joined it through the 2-hour margin. 41 were taken out.

The user's rules: similar photos stay together; even one photo can be an event "if it's in a new place with new people and not surrounded by photos with the same people"; "face identification should be a strong indicator that the event/trip album continues"; not every cat is the cat in the cat's album, so random cats go to a new `Cats`; a series of photos (shopping for glasses) is one album and even an event; every album should have a description; when in doubt, ask the LLM.

So S11 now works on groups:
1. **Sessions:** photos at most 15 min apart with no jump over 1 km were taken together and move as one. A burst or a document set that spans two sessions joins them.
2. **Joining an existing album:** a session joins it when it's inside the album's time span at its place. Within 2 h before or after the span, it joins only if it shares people with the album or looks like it (SigLIP 2 ≥ 0.80). A session never goes back to an album the reviewer took its photos out of.
3. **Events:** sessions up to 3 h apart at one place (≤ 5 km) are one event. So are sessions up to 36 h apart that share a person the owner doesn't photograph every day (seen on ≤ 6 days within ±60), and days away from home (one trip).
4. **The decision:** for each event, the local LLM gets:
   - what the photos show (captions), when and where they were taken, who is in them, and how many of those people appear nowhere else within 3 days;
   - the reviewer's notes;
   - the albums close in time with their summaries;
   - every thematic folder with its **profile**: one sentence on what it collects, written from its photos' captions (or, uncaptioned, from a line about a few of them).

   It answers: a new album (with a name in the owner's style), an existing album, a folder, or a new folder. A person's folder is offered only when that person is in the photos. A pet's album is offered only for its own animal: an undated album whose photos are mostly animals takes only photos with DINOv2 ≥ 0.55 to one of its own. On the sample, the cat's own photos are at 0.61 at the 10th percentile, and the six the reviewer rejected are all ≤ 0.42.
5. **Inside an album:** text pages go to its `Dokumenty/`, and a receipt or a document among other photos goes to its folder.
6. **Messenger photos** are decided the same way, photo by photo, always to a folder (P32).

The review shows one page per album or folder that receives photos. Each incoming photo is marked with the folder it comes from: no "To …" pages, no duplicates. `m` moves a photo or a burst to any album or folder of the library, or to a new album the reviewer names. S11 takes moves as given, and outs (`o`) as "anywhere but there".

## P36 — Orientation from the image, not only the EXIF tag (2026-11-03, user)

"A lot of photos don't seem rotated correctly." The two the user noted carry `Rotate 90 CW` in EXIF, and the pipeline applies it. The phone's sensor was wrong: it was pointing down, at a page and at a shelf. So the `orient` stage looks at the image:
1. **A screen learned from the library itself.** 1,500 photos, assumed upright once EXIF is applied, plus each one turned 90°, 180° and 270°: a logistic regression on SigLIP 2 embeddings learns the four turns. Classifying one view this way was too weak: 63–69 % on held-out photos, and it flagged 1 photo of 2,535. So each photo is looked at in all four turns, and the turn that makes the four views agree wins (the most likely t over the sum of log P(class t + k | view k)). On 200 held-out photos turned at random, this finds the turn 93.5 % of the time, with no mistakes above a margin of 4.
2. **The VLM checks what the screen flags.** On the sample's real turned photos (the two noted ones and 12 text pages), the screen points the same way as the text stage every time, but with low margins (0.4–2.5): a shot pointing down has no natural "up". So every photo the screen calls turned (margin ≥ 0.3) is asked about with the resident VLM, as the text stage does: "is it upright?", as it is and turned back. The turn is proposed only when the VLM prefers the turned-back photo.

Text pages keep the text stage's answer (P24). In the review, a proposed turn shows the photo turned, and `r` turns it 90° further. On apply, the turn becomes a lossless EXIF Orientation change. The cost per photo is three more SigLIP 2 passes, plus two VLM questions for each flagged photo.

## P37 — No home; trips by a rolling base; the library index from the library; the plan (2026-11-03, user)

- **No home.** "Over the years my home changed, so let's drop the idea of it. Make photos just another event wherever it happens."
  - There's no "Dom" folder and no "taken at home" rule any more. Photos taken at home are decided like any other event.
  - To tell a trip from everyday places, the **rolling base** is the median of the owner's daily positions within ±30 days. It follows a move, and each day counts once, so a trip's busy days don't pull it along.
- **Trips:** "a euro trip where you drive all the time" is still one trip "if the people stay the same and the subject of the photos doesn't change completely". So sessions away from the base, up to 30 h apart, are one trip wherever the road goes. They part only when no person is in common and the photos look nothing alike (mean SigLIP 2 embeddings < 0.6).
- **The library index from the library itself.** `OMP_LIBRARY_INDEX=self` builds it from the library's own DB instead of OneDrive's manifest. That's for a drive, a share or a clean-slate test ("do not match to any album that is not in the test photos"). Thematic folders then start empty and are proposed new ("all the theme directories created anew"). The default folders are only `Other/Cats` and `Other/Random`, not this code's category names.
- **The plan.** `omp plan` writes `OrderMyPhotos/plan.json`: every photo's move once the review is applied (kept → its album or folder; archived, bad or a copy → `Archive/OrderMyPhotos/`), plus the proposed turns. Applying it is a move per photo, in place ("do the moving in place as not to extend disk space"). For a test, `python3 -m omp.plan --links` builds the resulting folders as hardlinks next to the library: the same files, so no extra space, and the review page still finds the photos where they were.

## P38 — Starter folders with descriptions, three passes, Other/<year> (2026-11-03, user)

The user reviewed s5:
- "Obiad na zewnątrz" and "Most kolejowy" were folders but are events;
- "Sala gimnastyczna" should rather be "Skaleczenie na Akrobatyce";
- "Autumn Views" is a good folder but held people and a glass cup;
- `Random` duplicated photos of other groups;
- a new album of supplies was a bad album (it's part of the camping album);
- the bike-ride album was nice.

Their rules: "Random is random as in not making sense. To put some single photos that do not belong anywhere else, better create an Other in {Year}". And: precreate about 20 themed directories with descriptions to sort by, then sort in 2 or 3 passes, "better and better based on more and more refined albums and directories descriptions".

- **Starter folders** (`themes.yaml`, editable): 23 folders, each with what belongs and what doesn't. They're the defaults in a library that has none yet, and a library's own folders come on top. Examples: Documents, Paragony, Hardware, Screens, Notes, Maps, Notices, Food, Cats, Animals, Views, City, Nature, Sport, Vehicles, Home, Shopping, Clothes, Meme, Art, Screenshots.
  - `Pictures/Other/<year>` is for fine photos that belong nowhere else.
  - `Random` is only for photos that make no sense.
  - A new folder named like a starter folder in Polish (or a letter off: "Coty") is that folder.
  - "Inne" and its like mean `Other/<year>`, and "losowe" means `Random`.
- **Pass 1** decides every group as before.
  - A new folder needs photos from more than one day: a folder is for a subject that comes back, so a one-day "folder" is an event and becomes an album.
  - New folders whose photos look the same (mean SigLIP 2 ≥ 0.85) are one folder: the owner's cat came out as "Czarny kot" and as an invented "Coty".
- **Pass 2** corrects; it doesn't decide again. Deciding every group again flipped good answers: the bike ride the user liked went to Sport. So:
  - every destination is described from what it got, and a new album is renamed from that description;
  - a group is looked at again only when photos taken within a day that look like it (≥ 0.8) went elsewhere, or when a folder's group has a new album of its day to join;
  - a new album on a day the library already has an album for is asked about directly ("is this part of <album>?"). That's how the camping supplies joined their album.
- **Pass 3:** each photo in a folder is checked against the folder's own description, so no people in Views. Photos that don't fit (< 0.4) are decided again on their own.
- **Messenger photos** (revises P32): photos that arrived the same day and look alike (≥ 0.75) were sent together, so they're one group, decided like an event: the dinner outside, the railway bridge. A single one is still decided on its own, to a folder.
- **Events, said more clearly:** an occasion, an activity or a visit to a place is an event even in 2–3 photos without people (a dinner out, a walk to a bridge, a bike ride, an injury at training). A folder is for photos kept for their subject.
- **Cost on s5:** about 340–500 LLM calls at 0.9 s, 5–8 min for 437 photos.

## P39 — People veto a join; a burst moves as one; folders by their subject (2026-11-03, user)

The user reviewed the redone s5:
- two WhatsApp photos were in the camping album, but there's a "different person on the photos";
- two WhatsApp bursts of the dinner outside were half in its new album and half in `Other/2022`: "Photos that are already in Albums should not be duplicated in themed directories";
- two bursts in the cat's folder have no cat in them.

- **Faces veto a join into an album.** Some groups show people, and none of them is among an album's faces. That's someone else's event, however alike it looks, and the group doesn't join that album. A face counts when it's big and sure enough to be clustered (S8) and isn't the owner's. Two faces are one person at InsightFace cosine ≥ 0.35; the other person here scored 0.06–0.15. The veto applies to:
  - a messenger photo joining by its look (P32);
  - a session near an album that would join only by its look;
  - the albums offered to the LLM, existing and proposed;
  - the "is this part of that album?" question.

  A session taken inside an album's time span, at its place, still joins it whoever is in it: the owner photographs strangers at events too. The two photos had been a messenger group that the LLM put in the album because the album's description mentions a couple.
- **A burst moves as one.** In a messenger burst, the photos without a caption went by their look while the rest of the burst was decided. So, after every proposal is made, burst photos that went apart join the rest. An album wins over a folder; otherwise the burst follows its captioned photos, then the larger share. Pass 3 checks and moves a burst as one too. The private sibling (P26) keeps its own.
- **Folders by their subject.** The cat's folder was described from all of its photos, so the description took in a monitor unboxed at home that day, and the monitor then "fit". Now:
  - a folder is described by what most of its photos show;
  - a folder that is mostly animals takes only photos of an animal (the zero-shot kind or the caption), without asking.
- **The owner from the front camera.** Without a People folder of the owner's (a test set has none), the owner was unknown, so their own face counted as a stranger. Two problems in s5:
  - the clustering split them into four clusters, one of them only in WhatsApp photos others sent;
  - the railway-bridge photos of the owner were kept out of every album.

  So the owner is now found from the front camera:
  - a phone's front camera is the camera setting (model, f-number, 35 mm focal length) whose photos are at least 40 % selfies;
  - the owner is the cluster that is most often the biggest face in its photos (at least 3, and most of them);
  - clusters whose faces match the owner's (median best cosine ≥ 0.35) are the owner's too.

  On s5 this was f/2.2 at 25 mm, with 11 selfies in 18 photos, and it marked 3 of the owner's clusters. `people` now runs before `place`, so the joins that `place` makes before grouping already know the owner.
- **The owner's own pet isn't an event.** A new album has its pass-1 answer taken back and is decided again, without an album, when it:
  - is at home;
  - has nobody else in it;
  - is mostly an animal seen on 3 other days or more.

  In s5, "Kot na blacie" and "Koty na drzewie" had become albums. Animals seen only that day (a zoo) and trips stay albums.
- **"Part of that album?" asked with more to go on.** In s6 the user's album of a camping trip covers only its day. Its night (102 photos, 8.5 h earlier) and the morning walk became new albums. Asked with a one-line description of the album, the LLM said 0.51 and 0.43. The question now shows:
  - the album's own captions and dates;
  - the group's captions and how long before or after the album it was taken;
  - whether a person is in both.

  It says what "part of" means: the same outing, trip or occasion, including getting ready, the night before and the way back, but not another activity or place the same day. On s5 and s6 that gives 0.93 and 0.96 for those two, and 0.29 or less for every other pair. It's asked:
  - for each new album within a day of a library album, or of a bigger new album (two albums of one pet-shop trip became one);
  - the best yes ≥ 0.5 wins.

  At the end, an album of 1–2 photos without new people is decided again, however it got that small.

## P40 — Naming people and pets on a People page (2026-11-04, user)

The user: "as one of the first steps of the run (before sorting), let's create face clusters from all photos with faces, then build a webpage to name those faces with Id, Full Name and a Nickname, so that sorting, photo titles and albums know the names of the people. Albums should use mostly nicknames or first names in the titles." And: photo titles shouldn't be "Man with a cat" but "<owner> with <cat>" or "<name> i <owner> na szczycie góry" (names replaced here).

- **When:** `people` runs right after `faces`, before `place` and sorting (P39). It clusters every face and writes `people.html` into the bundle, next to the review's `index.html`, which links to it.
- **The page:** one card per face cluster, biggest first, with an Id (P1…), its face crops (big and frontal first), "Full name", "Nickname", "This is me" and "Not a person".
  - Clicking a face takes it out of the person.
  - **Merging:** the clustering splits people (s5: the owner in four clusters; s6: the person the albums name in eleven). The user ticks the cards and presses Merge, or types their Ids ("P1 P7 P8 …"). They become one card under the biggest, and a chip per merged card undoes it. The same full name on two cards merges them too. A merge without a name yet is exported as a group ("@P1"), so it holds until the person is named. Animals merge the same way, but never with people.
  - **"I don't know them"** (user, 2026-11-04) keeps a stranger as one anonymous person ("?P3"): never named, never suggested again, but still someone else when photos are sorted (another person, another event, P39). "Not a person" is for false detections, which are dropped. For animals, "Not an animal of ours" means what "I don't know them" means for people.
  - Suggestions from the album names and the front camera are shown with a "use" button.
- **Pets** are on the same page. The subject detector's animal boxes (≥ 15 % of the frame, score ≥ 0.5) are embedded with DINOv2 on the crop and clustered: 37 animal clusters from 107 boxes in s6. Animals seen in one photo only are folded away.
- **Saving:** names stay in the browser. They leave with the index page's "Export all decisions" (or the page's own "Export names"), and `omp labels --file` imports them. They're stored per face or animal box (`name_label`, keyed by photo and box), not per cluster, so they hold when the faces are clustered again.
- **Applying them** (`people`, step 0, before People folders and album names):
  - a cluster is whoever most of its named faces are;
  - faces taken out leave it;
  - clusters with one name merge;
  - an unnamed cluster whose faces match a named person's (median best cosine ≥ 0.35; pets ≥ 0.6 on DINOv2) is that person.

  "This is me" makes the owner, ahead of the People folder and the front camera; without a name it's still the owner.
- **Using them:**
  - **albums** name people by the nickname, else the first name ("z Anią"), and named pets too ("z Filemonem"); the owner is never named in an album;
  - the pet the user named is the owner's pet: a new album at home that is mostly that pet isn't an event (the P39 rule, which by look alone needs the animal on 3 other days);
  - **captions** get "Recognized in the photo: Jan (the owner, on the left), Ola (on the right); the cat Filemon", with positions only when there are several. They're asked to use the names in the right Polish forms, and a photo is captioned again only when who it shows has changed.
- *Tested* on a copy of the s6 DB with test names:
  - one named cluster pulled in 8 more clusters of the same face (89 faces);
  - a face taken out left;
  - "This is me" without a name made the owner;
  - a named cat gathered 37 boxes;
  - the caption context read "Ania", "the owner".

## P41 — Another event inside an album's time; places from the backgrounds; fixed folder descriptions (2026-11-04, user)

The user, on the s6 review: the week-long album "<name>" (12–19 Nov) is fine, but its Event 11 "is clearly something that deserves its own album. It's a birthday party, it has much more people, a different place. It's fine to join photos before and after this event, but those are clearly something separate."

34 party photos (6 recognized people and many unknown faces) had joined the album only because they were taken inside its time span, with no GPS. Their best face match to the album was 0.21, well below the 0.35 of one person. And because `place` had joined them, they were captioned with the album's name as context, which put its person into the captions ("in <name>'s living room").

- **No join by time for strangers:** the P39 face veto now holds for joins by time too. A session whose people are all strangers to an album is decided as an event.
- **Places from the backgrounds** (user: "describe a photo's place from the background: inside or outside, the style of the furniture, the colour of the walls, the lights. A new place doesn't mean a new album, but like the GPS position, together with other factors like the number of people, it may indicate a separate event"):
  - captions gain `indoors` and `setting`, e.g. "apartment kitchen with purple cabinets, blue ambient light";
  - a session showing a crowd (≥ 4 people, at least twice as many as the album's photos within 12 h) doesn't join by time when the LLM, given both sides' backgrounds, says it's another place. The party: 9 people, P(same place) 0.01;
  - a new place alone changes nothing. The backgrounds also go into the decisions and "part of that album?", whose wording now says that on a trip the place changes as it goes.
- **Captions redone when their album changes:** a caption is written again when the album it's described with changed, not only who it shows. The party's 15 kept photos were rewritten ("<friend> z tortem urodzinowym").
- **Things a re-run showed, fixed:**
  - **Album summaries** (captions pass 2) were rewritten each run at temperature 0.2. A new wording of the trip album's summary sent 60 of its photos to `Other/2022` and `Random`. They're now at temperature 0 and written again only when the album's captions change.
  - **An album answered but not offered** silently became `Other/<year>`. Names are now matched without the path, case or Unicode form, and what's still unmatched is counted in the log.
  - **Hidden review pages:** a photo kept back from captioning for nudity, joining an album, kept its page from the review. It no longer counts as uncaptioned.
- **Thematic folders keep a fixed description** (user: "a thematic folder is created for a specific type of photos, and that doesn't change"). Pass 2 no longer rewrites them from what a run put in them; only new folders and new albums are described that way. This also saves the calls for library albums, whose descriptions were written and never used.
- **The pet the user named** (P37, P40):
  - the decision prompt names it ("a new_folder named "<cat>"") when it is in most of a group's photos;
  - the pet's folder takes only photos of that pet, checked after every pass;
  - photos of it during an album of the owner's that shows it too go to that album (the user's week album "is fine" with its cat photos).

  Without these, the first try put accidental shots and receipts in the cat's folder. A check inside every decision moved 72 trip photos to `Other/2022`, so the checks run after the passes instead.
- **Shopping for a trip is its own event** (user, on the pet-shop trip that joined the camping trip as "getting ready" at 0.75: "own album again"):
  - "part of that album?" now counts packing and setting off as part of a trip, but not shopping for it or another errand before it;
  - for messenger photos it says they were received that day with no time, instead of a midnight time;
  - the first wording ("an errand is an event of its own") split the pet-shop trip into two albums, because it read the other half as the errand.
- **A group's own new album is offered in pass 2:** "keep it there" named an album the group wasn't offered, and 10 trip photos went to `Other/2022`.
- **S11 reads English only** (user, 2026-11-04): the album namer got Polish titles, and now gets English ones. Album names stay Polish.
- **Messenger photos sent the same day** as a new album made of that day's messenger photos join it when "part of that album?" says so, burst by burst. The user noticed photos of the pet-shop trip outside its album: a spoon and a harness that didn't look like the rest, decided alone to folders. 6 more joined, 19 in all.
- **The namer names a place** only from the GPS or a name readable in the photos. A geyser without GPS became "Islandia".
- *Result on s6:* the party is a new album ("Urodziny <friend>", 34 photos). The album keeps the evening before and the night walk after. The trip is whole, and the pet-shop trip is its own album again (19 photos). Besides that, the changes from the accepted run are:
  - the cat's own photos are in its folder (11, 5 of them from `Cats`);
  - new albums' names vary a little.

## P42 — Two folders are left as they are; descriptions in both languages, sorting in English (2026-11-04, user)

The user, on the folders with no rule: "Leave Pictures/Private as is. Leave Code as is." On the language (open question 1): "Both English and Polish descriptions and tags, but for AI sorting use only English."

- **A `leave` policy** (`policy.yaml`) for `Pictures/Private` and `Code`. The inventory doesn't enter these folders, so their files are never:
  - read or hashed;
  - compared as copies or used as examples;
  - moved;
  - served by the web app (P44).
- **Folders the design already called "not personal photos"** whose real names the rules missed are now `ignore`: `Obrazy/Tapety` and `Obrazy/wallpaper` (wallpapers), and `Obrazy/thermo_tc2c` (the thermal camera). `Obrazy/Screenshots 1` is `describe`, like `Screenshots`.
- **The other folders without a rule stay `unsorted`** (the default) until the user says otherwise:
  - `Obrazy/Historia` (1,314 media files), `Dokumenty` (443), `Video` (345);
  - `Obrazy/Random`, `Meme`, `9GAG`, `Download` and `Quick Share`;
  - `Projects`, `Desktop`, `Main` and a .NET folder;
  - an album folder outside `Albums/`, which P30 relocates.
- **Language:** captions already write the title and description in both languages, and every tag maps to a vocabulary entry that has both names. S11 reads only the English fields: the one prompt that read `title_pl` was switched in P41. Album names stay Polish. They're the owner's naming style, an output of the sorting, not an input to it.

## P43 — A run that takes days survives stops, crashes and reboots (2026-11-04, user)

The user, before the whole-library run: "resilience to breaks and restarts. This takes a long time and we may need to stop and restore the process at any time."

**What was there:**
- Most per-photo stages committed only at the end: inventory, previews, DINOv2, SigLIP, Q-ReAlign, nudity, faces and depth. A stop at hour 16 of Q-ReAlign's ~17 would have redone all of it.
- LLM calls failed on the first 502, and a llama-swap restart takes ~100 s.
- One unreadable photo stopped a GPU stage.
- Runs were `nohup setsid` scripts, so a reboot ended them.

**Now** (`runtime.py`):
- **Commits** every 500 photos or 10 s, always between photos. Short transactions also let the web app write labels during a run.
- **A stop** (SIGTERM or SIGINT, i.e. `docker stop`, Pause in the web app, Ctrl-C) ends a stage loop after the photo in hand. Outside a loop (model loading, the CPU stages that redo their whole run anyway) it stops at once. The exit code is 143, and the same command resumes.
- **A bad photo** is logged as an `error.<stage>` signal and skipped. Twenty in a row stop the stage, since then it's the model or the GPU.
- **llama-swap calls** are retried for up to 15 minutes on connection errors, timeouts, 429 and 5xx. A 4xx raises `ValueError` at once, so captions skip that photo.
- **Process pools** cancel their queued work on a stop.
- **One lock per DB:** a second `omp` on the same library exits.
- **Several stages per call:** `omp inventory previews signals`.
- **Hashing and EXIF** are committed in batches, and exiftool runs in parallel threads.
- **Face clustering** runs per connected component of the "closer than the threshold" graph (`people.cluster`). Average linkage never merges clusters with no pair under the threshold, so the result is the same: s6 gives 69 clusters, ARI 1.000. Memory stays bounded: 80k synthetic faces took 53 s and 0.8 GiB, where the old way needs ~25 GB for 80k. A component over 30k faces is clustered in slices, which are then merged by their mean faces (on s6, forced: ARI 0.90).

**Tested** on a copy of s6 under the web app:
- a redeploy in the middle of SigLIP: it stopped cleanly and resumed with the 216 photos left;
- Pause and Resume in the middle of Q-ReAlign;
- `kill -9`: retried after 30 s, with at most 10 s of work lost.

**Not yet:**
- The stages that recompute their whole run (group, rank, people, albums) redo it on resume.
- The albums stage keeps no LLM answers across runs, which is about 8 h at full scale.

## P44 — A web app runs and shows the pipeline, from the repo's own clone on Mimir (2026-11-04, user)

**The user's requests:**
- "an OrderMyPhotos webpage with start/pause button, progress status, link to results. Hosted on Mimir with the rest of the tools. The final run will use Claude only for monitoring via API and SSH, but the tool itself should be a standalone solution running from the OrderMyPhotos repo on Mimir. This laptop will be used only for monitoring."
- "full web app management, merge all the manual review steps pages, create api server";
- "the process should show a lot of details, what steps are running, how many photos were processed, how much system resources is used and more."

**`omp serve`** (`web.py`, `web.html`; FastAPI, already in the image) is a page and a JSON API: `/api/status`, `/api/run/start`, `/api/run/pause`, `/api/log`, `/api/history`, `/api/labels`, and the API docs at `/docs`.
- **The pipeline is its child process.** The wanted state is kept in `control.json` next to the DB, so after a crash, a redeploy or a reboot the run resumes on its own.
  - A failed run is retried after 30 s, 2 min, 10 min and 30 min. After 5 failures in a row it's marked failed.
  - The count starts again after 10 minutes of running.
- **Three pipelines**, because each needs the one before and the user in between:
  1. signals per photo;
  2. people, bursts and picks; then the user names people;
  3. captions, which use the names, and albums, which read the captions.
- **What it shows:**
  - the stage and step, photos done of the total, the speed overall and over the last minute, and the time left;
  - the steps finished, the skipped photos with their errors, the stage timings and the library's counts per signal;
  - the run's CPU, memory, disk I/O and process count; the host's CPU, memory and free disk;
  - the B50's load (from the xe driver's idle residency) and the run's own B50 VRAM and engine load (from its DRM fdinfo);
  - the 4090 and the B50's total VRAM, from the host (mimir `mimir-gpu-stats`);
  - the models llama-swap has loaded, and charts of the last 10 minutes.
- **No NVIDIA device in the container:** with the NVIDIA CDI device there, PyTorch saw no XPU (measured), so every B50 stage would run on the CPU.
- **The results:** the bundle's pages, served by the app from the library, read-only, with `leave` folders hidden. `/api/labels` imports a page's export (decisions or names) at once.
- **Next:** the review pages (albums, the People page, proposals) move into the app and save through the API instead of a download.

**Deployment:**
- `compose.yaml` at the repo root, with two env files of host paths:
  - `deploy/mimir.env`: the whole library, read from the "pristine" snapshot so files don't move under a run while the hourly pull goes on;
  - `deploy/test.env`: a test set next to it.
- The code is mounted from the clone, so `git pull` and a restart deploy a change.
- It's tailnet-only, through Tailscale Serve (mimir D25).

## P45 — Repeatable S11 answers; a neighbour table and a sure-first loop, as a prototype (2026-11-04, user)

The user: "Do we need more phases? Something that runs in a loop and tries to put a photo into an album, and if it's not sure, waits for another pass, so maybe the descriptions will be better and we'll be able to decide. Also maybe find a fast way of comparing any photo to all others, maybe with a graph DB, so that similar photos are found even in different directories." (P42–P44 and P46 are the full-run work.)

- **Why S11 wasn't repeatable:** llama-swap's `:instruct` alias sets temperature 0.7, top-p 0.8 and presence penalty 1.5 on every request (its `setParamsByID` filter). So every S11 call "at temperature 0" was really sampled. Two runs of the same code moved 11 photos and renamed every new album. The yes/no answers weren't affected, because their probabilities are read before sampling.
  - S11 now asks the same loaded model by its own id (`qwen3.8-27b`) with thinking off, where temperature 0 holds. It does this through `VLM(cfg.llm, repeatable=True)` (P46); a wrapper in `albums.py` did it before.
  - It also turns off the server's prompt cache. Reusing the start of the previous prompt still changed long answers after some words, and those changes reached later questions. With the cache off, two runs ask the same 101 questions. Only reason texts can still differ (6 of them), never a placement. It costs about 15 s per run on s6.
  - Test runs get a fixed hash seed (`PYTHONHASHSEED=0`), because some prompts are built from sets.
  - Captions went through the alias too, so they were written at temperature 0.7 with a presence penalty, not at 0.2. Since P46 they get the temperature they ask for; the s6 captions are still the earlier ones.
- **The neighbour table** (`neighbours.py`): each photo's 20 most alike photos by SigLIP 2, from any folder, in a `neighbour` table of the same DB. No graph database: the queries go one or two steps deep. On s6 it's one matrix product. The full library (~173,000 photos in the mirror) needs a chunked GPU path or FAISS, which is part of the full-run work.
- **The sure-first loop** (`OMP_SORT=loop`, in place of pass 2):
  - **Places to try** for each group: pass 1's pick, the library's albums within a day, the new albums placed so far, an album of its own, and the places of its look-alikes.
  - **One question per place,** answered yes or no: "part of that album?", "an event of its own?" or "belongs in this folder?". Each comes with where the look-alikes are, as evidence.
  - **Albums first:** when an album wants the group (≥ 0.5), the albums compete among themselves. Otherwise an album of its own and the folders compete. "Part of the trip" and "an event" were both true of a trip's night (0.96 and 0.9), so weighed together they never pulled apart.
  - **Placing:** a group is placed when its best answer is ≥ 0.8 and 0.3 ahead of the next one. The others wait for the next round, which knows where the placed groups went.
  - **After 4 rounds,** or a round that placed nothing, the rest take their best place, or pass 1's pick when nothing scores 0.5. They're marked "unsure" in their reason.
  - **Then the usual steps:** new albums are renamed and joined to their event's album, pass 3 runs, then the rules after it.
- *Result on s6:*
  - repeatable: two loop runs place every photo the same;
  - the same placements as the passes;
  - 15 groups sure (14 in the first round, 1 in the second) and 8 unsure: the pet-shop trip's two halves (0.79 and 0.54, merged afterwards), the beer shopping (0.18) and four single photos;
  - 114 calls in 107 s, against 101 calls in 116 s for the passes.

  With temperature 0 really holding, the beer shopping became its own album (8 photos), where the user's last review had it in Shopping.
- **Adopted** (user, 2026-11-04): the loop is the default, and `OMP_SORT=passes` brings back the old pass 2.
  - Photos the loop wasn't sure of carry an "unsure" badge on their review page, with the best answer and the next one.
  - The page's Show menu lists them on their own.
  - The index counts them per album and folder.

  On s6 that's 11 photos of the pet-shop trip, 5 of the beer shopping and five single photos. On this small set the loop changes no placement. Its rounds should matter where many groups depend on each other, and the look-alike evidence where the library has its own albums and folders.

## P46 — LLM calls ask for the model by its plain name, so their temperature holds (2026-11-04, measured)

The S11 session found that llama-swap's `qwen3.8-27b:instruct` alias has a `setParamsByID` filter (mimir `stacks/llama-swap/config.yaml`). It sets temperature 0.7, top_p 0.8 and presence_penalty 1.5 on **every** request. So every "temperature 0, repeatable" JSON call in the pipeline was really sampled at 0.7, and so was every caption at 0.2. Two s6 runs moved 11 photos between them.
- `vlm.py` now asks for `qwen3.8-27b`: the same loaded model, without the filter. Thinking is off through `chat_template_kwargs`, and the caller's temperature holds.
- Results stay recorded under `qwen3.8-27b:instruct`, so nothing done is redone silently. The yes/no logprobs (judge, floor, orientation checks) are identical through either name (measured: the same top-5 to 4 decimals). Captions written before this were sampled at 0.7. Whether to redo them is a captions decision.
- `VLM(..., repeatable=True)` also turns off the server's prompt-prefix cache, which made long answers drift (P45). The same question then gets the same answer (measured twice at temperature 0). It costs a full prompt recompute per call, so captions don't use it; S11 does.

## P47 — A wizard over sessions, with every review in it; every album reviewed the same way (2026-11-04, user)

**What the user asked for:**
- "Start should start the process, and pause when user input is required: reviewing and naming faces, reviewing sorting and archived photos, and the delete decision at the end. This page should be like a wizard, guiding through phases and the work needed by the server or a reviewer."
- "The photo review page needs improvements: cleaner, hide unneeded text, think about whether all features are still needed (e.g. accept). A reviewer may exclude some photos from the albums, so another sorting run will be needed to put them somewhere else, and then another manual review that takes the new sorting into account but doesn't redo already confirmed photos."
- "Remove the idea of albums and folders receiving photos: all albums should be treated equally in the review. All may get new inclusions or be dismantled, and their photos will need resorting."
- "Let the web app start by specifying a root directory for the session (only one session will run at one time)." Future test sessions use the app too.

**Sessions** (`sessions.py`):
- A session is one root directory: the library's pristine snapshot, or a test set in `/srv/mimir/cache/photos/tests/`.
- Each session has its own DB, status, log and review state in `<state>/sessions/<id>/`, and its cache and bundle in `<cache>/<id>/`.
- One session is active at a time. A new one is made from the app by picking a folder under the allowed roots.
- An adopted DB keeps its run id (`run`): s6 became a session with its existing DB.

**Phases:**
1. read (server);
2. signals (server);
3. people (server);
4. **names** (you): the People page saves straight to the app;
5. picks (server: people again with the names, then place, group, rank, judge);
6. sort (server: captions, albums);
7. **review** (you);
8. resort (server: albums), back to the review, as often as needed;
9. plan (server: albums once more to apply the confirmations, then plan);
10. **final** (you).

Start runs the server phases one after another and stops at the next review. "Done, go on" continues. Any finished phase can be redone. The combobox of pipelines is gone.

**The review** (`review.py`, `review.html`):
- **Units:** every album and folder is one unit, whether it exists or is new. It shows the photos that will be in it: its own photos that stay, and those the sorting put there. ` PRIV` and `/Dokumenty` are parts of their album's unit.
- **Controls:**
  - a click opens a photo large; a right-click (or Space) keeps or archives it;
  - O takes it out, M moves it to a chosen album or folder or a new album name, Z shows it large;
  - the unit can be renamed (an album keeps its date) or dismantled: all its photos go back to sorting, and nothing is sorted into it again;
  - **Confirm** pins every photo in the unit: a `place` label for its place, and a `verdict` label for keep or archive.
- **Saved at once** (user: "all picks and decisions should be saved at once and remembered between devices and browsers"):
  - keep/archive, out, put back and move are each written as a label the moment they're made;
  - a rename is kept on the server until Confirm;
  - nothing lives in the page, so there's no leave-page warning;
  - the People page in the app (`/people`, in the review's design: a click shows a face's photo full screen with the person's other faces on the right, a right-click takes a face out, the arrows move between cards) loads its state from the session and saves it on every change. Only the faces it shows change in the DB, and a first open starts from the DB's names (faces taken out included).
- **The large view** (Enter or Z; Confirm is Ctrl+Enter, so it isn't pressed by accident) fills the screen, so nothing changes size from photo to photo: ←/→ go on to the previous or next photo, Space and O work there too, and the burst is shown in a column on the right of the photo. On the page, the selection scrolls into view, and ↑/↓ move by row.
- **Finished** (user: "a button Finished that will accept all the rest as is and move on with the process"): on the review page, the People page and the wizard. It confirms every unit not confirmed yet as it is (verdicts and places pinned, renames kept), then goes on as Done does.
- **One page** (user: "let's not open review and plan in a separate tab, let's make it all more single-page-app feel"):
  - one header with the app's icon, Progress · People · Review · Plan, the state and the session;
  - the views load under it at `#/people`, `#/review/<album>` and `#/final`, with Back and Forward;
  - each view runs in its own shadow root, so their styles and ids stay apart;
  - between two server phases the state is "starting": the next phase starts at once, so there's no "stopped, exit 0" moment.
- **What Done does:** Done needs every unit confirmed. Photos taken out (or from dismantled units) go to the `resort` phase. After the resort, only units that received photos are open again, and their new photos come first. What was confirmed is folded away as "Already confirmed" and isn't asked again.
- **The final decision:** the plan's counts, archived photos with their size, and where photos go; then "keep the archived photos in `Archive/OrderMyPhotos/`" or "delete them once the plan is applied". Applying the plan to OneDrive (S9) is still to build.

**The sorting honours the review** (`albums.py`):
- The reviewer's latest word on a photo wins: out, move or place.
- A photo confirmed in its own album stays and isn't sorted again. One confirmed anywhere else goes exactly there, its PRIV or Dokumenty part too. Neither is moved by "a burst moves as one".
- A photo moved by hand still goes to the album's private part when it shows full nudity.
- A dismantled unit is banned for every photo.

**Gone from the review:**
- "Accept" (proposal labels were never read) and "Trust automatic picks": Confirm is the one confirmation.
- Burst editing, category and rotate, which no stage read.
- "bad", now the same as archive.
- Load and export of labels files: the app saves.
- "Not an album", now Dismantle.
- The separate proposal, copies, PRIV and Dokumenty pages.

The static bundle (`omp report`) is still there for a library someone reviews offline (P21), but it's no longer a phase.

**Tested** on a copy of s6 through the API:
- **Round 1:** dismantled a new album (34 photos), took one photo out of a folder, renamed a new album, confirmed the rest. That left 35 photos to sort again.
- **Round 2:** came back from the resort with only the units that received photos open.
- Then the plan and the final decision; the results are in sample-runs.md.

## Open — for the user

1. ~~**Language** of titles, descriptions and tags.~~ Resolved by P42: both languages; S11 reads English only.
2. **Archive and deletion:** is `Archive/OrderMyPhotos/` on OneDrive right, or should culled files leave OneDrive for a local archive on Mimir? How long before a delete?
3. **Edited copies:** keep both the edit and the original (the default), or only the edit?
4. **Live mode auto-apply:** which operation classes may skip review once proven safe (exact copies, metadata, bursts with a clear winner)?
5. **Video re-encoding:** yes or no, and is AV1 acceptable for playback on your devices?
6. **Folder policy:** confirm which top-level folders are `unsorted` (album candidates), `describe` or `ignore`. `Pictures/Private` and `Code` are left as they are (P42); the folders still on the default are listed there.
7. ~~**The public GitHub repo** had personal photos in its history.~~ Resolved by P15.
8. **Category albums:**
   - Are the default folders and names right (design.md S3b)? Should new ones be Polish (`Info/Dom`, `Info/Notatki`) or English like `Info/Documents`?
   - Should trip receipts and tickets go to Receipts (the default), or stay with the trip?
   - Any retention rules (e.g. suggest archiving receipts after 3 years)?
