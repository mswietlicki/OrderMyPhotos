"""People cards' faces measured by a second model, InsightFace glintr100 (antelopev2), for `people_merge.py --emb2` (P71).

Two steps, as the app container has the API and the CUDA image has the GPU:

    # 1. up to 30 faces per people card, the same ones every time (by the key's hash), from the app's API
    docker exec -i omp-omp-1 python - keys /state/sessions/onedrive/glint-keys.json < scripts/faces_glintr100.py
    # 2. each one cropped from its preview with room around it, found again by antelopev2's detector (for its 5
    #    landmarks), aligned and embedded, on the 4090 (unload qwen first); no network, capped at 8 GB
    docker run --rm --init --network none --memory 8g --memory-swap 8g --device nvidia.com/gpu=all -u 1001:1002
      -v /srv/mimir/models:/models:ro -v /srv/mimir/cache/photos/sessions:/cache:ro
      -v /srv/mimir/data/photos/sessions/onedrive:/out -v $PWD/scripts:/scripts:ro --entrypoint python omp-cuda:0.4
      /scripts/faces_glintr100.py embed /out/glint-keys.json /out/faces-glintr100.npz /cache/onedrive/cache/preview
    (one command)

31,213 faces of the full library's 3,921 cards took 128 s on the 4090 (1.5 GB of RAM); 132 weren't found again.
"""
import sys

if sys.argv[1] == "keys":
    import hashlib
    import json
    import urllib.request

    CAP = 30
    with urllib.request.urlopen("http://127.0.0.1:8790/api/people/cards") as r:
        cards = json.load(r)["people"]
    keys = []
    for c in cards:
        keys.extend(sorted((f["k"] for f in c["faces"]), key=lambda k: hashlib.sha1(k.encode()).hexdigest())[:CAP])
    json.dump(keys, open(sys.argv[2], "w"))
    print(f"cards {len(cards)}, faces to measure {len(keys)}, photos {len({k.split(':')[1] for k in keys})}")
    sys.exit(0)

sys.argv = sys.argv[1:]          # embed <keys.json> <out.npz> <preview root>

import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from insightface.model_zoo import model_zoo
from insightface.utils import face_align
from PIL import Image

keys = json.load(open(sys.argv[1]))
out, root = sys.argv[2], sys.argv[3]
M = "/models/insightface/models/antelopev2/"
prov = ["CUDAExecutionProvider", "CPUExecutionProvider"]
det = model_zoo.get_model(M + "scrfd_10g_bnkps.onnx", providers=prov)
det.prepare(ctx_id=0, input_size=(320, 320), det_thresh=0.3)
rec = model_zoo.get_model(M + "glintr100.onnx", providers=prov)
rec.prepare(ctx_id=0)
print("providers:", det.session.get_providers()[0], rec.session.get_providers()[0], flush=True)

by = defaultdict(list)
for k in keys:
    _, pic, box = k.split(":", 2)
    by[pic].append((k, json.loads(box)))


def load(pic):
    try:
        img = Image.open(f"{root}/{pic[:2]}/{pic}.jpg").convert("RGB")
        return pic, np.asarray(img)[:, :, ::-1].copy()
    except Exception:  # noqa: BLE001
        return pic, None


ok_keys, embs, batch, bkeys = [], [], [], []
missing = nodet = 0
t0 = time.time()


def flush():
    global batch, bkeys
    if batch:
        f = rec.get_feat(batch)
        embs.extend(f / np.linalg.norm(f, axis=1, keepdims=True))
        ok_keys.extend(bkeys)
    batch, bkeys = [], []


def loaded():
    """The photos decoded by 8 threads, at most 32 ahead of the GPU (all at once was 60 GB and an out-of-memory kill)."""
    from collections import deque
    it = iter(list(by))
    with ThreadPoolExecutor(8) as ex:
        pending = deque(ex.submit(load, p) for _, p in zip(range(32), it))
        while pending:
            yield pending.popleft().result()
            nxt = next(it, None)
            if nxt is not None:
                pending.append(ex.submit(load, nxt))


if True:
    for n, (pic, img) in enumerate(loaded()):
        if img is None:
            missing += len(by[pic])
            continue
        H, W = img.shape[:2]
        for k, b in by[pic]:
            x0, y0, x1, y1 = b[0] * W, b[1] * H, b[2] * W, b[3] * H
            side = max(x1 - x0, y1 - y0) * 2.0
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            sx, sy = int(round(cx - side / 2)), int(round(cy - side / 2))
            s = max(int(round(side)), 8)
            crop = np.zeros((s, s, 3), np.uint8)              # padded where the square leaves the photo
            ax0, ay0, ax1, ay1 = max(sx, 0), max(sy, 0), min(sx + s, W), min(sy + s, H)
            if ax1 <= ax0 or ay1 <= ay0:
                nodet += 1
                continue
            crop[ay0 - sy:ay1 - sy, ax0 - sx:ax1 - sx] = img[ay0:ay1, ax0:ax1]
            bboxes, kpss = det.detect(crop, input_size=(320, 320))
            if bboxes is None or len(bboxes) == 0:
                nodet += 1
                continue
            c = np.array([s / 2, s / 2])
            centers = (bboxes[:, :2] + bboxes[:, 2:4]) / 2
            i = int(np.argmin(np.linalg.norm(centers - c, axis=1)))   # the face the box was about: the one in the middle
            batch.append(face_align.norm_crop(crop, kpss[i], 112))
            bkeys.append(k)
            if len(batch) >= 128:
                flush()
        if n % 2000 == 0:
            print(f"photos {n}/{len(by)}, faces {len(ok_keys)}, {time.time() - t0:.0f} s", flush=True)
flush()
np.savez(out, keys=np.array(ok_keys), embs=np.stack(embs).astype(np.float16))
print(f"done: {len(ok_keys)} of {len(keys)} faces in {time.time() - t0:.0f} s; preview missing {missing}, face not found again {nodet}")
