"""S7 captions and tags: one JSON-schema request per kept photo to the local VLM, with a computed (not
rolling) context block. Titles and descriptions come in Polish and English until the language is
decided (decisions.md, open question 1). Free tags are mapped onto vocab.yaml with Qwen3-Embedding and
checked against the image with SigLIP 2; what doesn't map goes to review."""

import hashlib
import json
import time
from datetime import datetime
from pathlib import PurePosixPath

import httpx
import numpy as np
import yaml

from .config import PACKAGE_DIR, Config
from .db import get_vecs, log_timing
from .text import OCR_TAG, ocr_needed
from .vlm import VLM, image_part

VERSION = "caption@1"
EMBED_MODEL = "qwen3-embedding-4b"
MAP_MIN = 0.80           # Qwen3-Embedding cosine (tag vs. one label or synonym) to map a free tag
SIGLIP_KEEP = 0.35       # a mapped tag must beat this share of the vocabulary on SigLIP similarity

CATEGORIES = ["none", "documents", "receipts", "labels", "specs", "screens", "home", "to_remember", "maps_topos",
              "food", "shopping", "memes"]
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title_pl", "title_en", "description_pl", "description_en", "tags", "scene", "activities",
                 "objects", "text_in_image", "purpose", "category", "people_count", "indoors", "setting"],
    "properties": {
        "title_pl": {"type": "string"}, "title_en": {"type": "string"},
        "description_pl": {"type": "string"}, "description_en": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "scene": {"type": "string"},
        "activities": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "objects": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "text_in_image": {"type": "string"},
        "purpose": {"type": "string", "enum": ["memory", "reference", "media", "junk"]},
        "category": {"type": "string", "enum": CATEGORIES},
        "people_count": {"type": "integer"},
        "indoors": {"type": "string", "enum": ["indoors", "outdoors", "unknown"]},
        "setting": {"type": "string"},
    },
}
SYSTEM = ("You write captions for a private photo library. Describe only what is visible. Be concrete and plain: "
          "no flowery words (serene, tranquil, breathtaking, harmonious), no guesses stated as facts, no camera, "
          "file or timestamp details. Polish text must be natural, correct Polish.")
INSTRUCTIONS = """Context about this photo (use it only where the photo agrees with it):
{context}

Write:
- title_pl / title_en: 2-6 words naming the subject, in Polish / English.
- description_pl / description_en: 1-3 sentences, at most 50 words, saying what is in the photo and what is happening.
- tags: 5-12 English search tags: kind of place, landscape features, activities, notable objects, events. No colours unless colour is the subject. Multi-word tags are fine ("ski touring").
- scene: one short phrase. activities, objects: short lists.
- text_in_image: readable text, verbatim, or "".
- purpose: memory (people, places, events, scenery), reference (taken to keep information: a document, receipt, label, map, information board, screen), media (meme, screenshot), junk (accidental, nothing recognizable).
- category: for reference or media photos one of the listed categories, otherwise "none".
- people_count: number of people visible.
- indoors: indoors, outdoors, or unknown (a close-up, a screenshot, a document).
- setting: the place, read from the background, so that photos of one place can be told from another's: the
  kind of place (kitchen, living room, office, shop, street, forest, beach), then what marks it: the colour of
  the walls, the style of the furniture, the light (daylight, lamps, coloured lights, fire). English, at most 20
  words, no people. "unknown" when the background doesn't show the place.
- Names: when the context says who is in the photo (face recognition) or names an animal, call them by those
  names in the titles and descriptions, in the right Polish forms ("Jan z Filemonem", "Ola i Jan na szczycie
  góry"), where it's clear who is who. Never give anyone else a name."""

SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer",
           8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}


def part_of_day(h: int) -> str:
    return ("night" if h < 5 else "early morning" if h < 8 else "morning" if h < 11 else "midday" if h < 14
            else "afternoon" if h < 18 else "evening" if h < 22 else "night")


def album_title(album: str) -> str:
    name = PurePosixPath(album).name
    return name[11:] if len(name) > 11 and name[:4].isdigit() and name[10] == " " else name


class TagMapper:
    def __init__(self, cfg: Config):
        spec = yaml.safe_load((PACKAGE_DIR / "vocab.yaml").read_text(encoding="utf-8"))
        self.paths = list(spec)
        self.pl = {p: v[0] for p, v in spec.items()}
        self.syn = {p: v[1:] for p, v in spec.items()}
        self.url = f"{cfg.llm}/v1/embeddings"
        self.client = httpx.Client(timeout=300)
        # One vector per label and synonym, compared phrase to phrase without an instruction: on the sample
        # that separates right (>= 0.80) from wrong mappings far better than one vector per entry.
        self.items = [(p, s) for p in self.paths for s in [p.split("/")[-1]] + self.syn[p]]
        self.vocab = self.embed([s for _, s in self.items])
        self.cache = {}

    def embed(self, texts):
        out = []
        for i in range(0, len(texts), 32):
            r = self.client.post(self.url, json={"model": EMBED_MODEL, "input": texts[i:i + 32]})
            r.raise_for_status()
            out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        v = np.array(out, np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def map(self, tags):
        new = [t for t in tags if t.lower() not in self.cache]
        if new:
            for t, v in zip(new, self.embed(new)):
                sims = self.vocab @ v
                j = int(sims.argmax())
                self.cache[t.lower()] = (self.items[j][0], float(sims[j]))
        return [(t, *self.cache[t.lower()]) for t in tags]


STOP_TAGS = {"outdoors", "outdoor", "nature", "daytime", "photo", "image", "outdoor recreation", "travel", "scenery",
             "view", "text"}


def is_name(tag: str) -> bool:
    """Proper nouns (a town, a mountain range, a venue) are names, not vocabulary concepts."""
    return tag[:1].isupper() and any(c.islower() for c in tag)


class SiglipCheck:
    """Scores each vocabulary entry against a photo's stored SigLIP 2 embedding."""

    def __init__(self, cfg: Config, paths, syn):
        import torch
        from transformers import AutoModel, AutoProcessor

        from .vision import SIGLIP, device, free, siglip_text

        dev = device()
        model = AutoModel.from_pretrained(SIGLIP, dtype=torch.float16 if dev != "cpu" else torch.float32).to(dev).eval()
        processor = AutoProcessor.from_pretrained(SIGLIP)
        texts = [f"a photo of {(syn[p] or [p.split('/')[-1]])[0]}" for p in paths]
        self.text = siglip_text(model, processor, texts, dev).cpu().numpy()
        self.paths = paths
        del model
        free()

    def shares(self, emb) -> dict[str, float]:
        """For each vocabulary entry, the share of the vocabulary it beats on this photo (0..1)."""
        sims = self.text @ emb
        ranks = sims.argsort().argsort()
        return {p: float(r) / (len(self.paths) - 1) for p, r in zip(self.paths, ranks)}


def knew(caption: dict) -> str:
    """Who a caption was written knowing about (P40); the first ones keep it only in their context line."""
    if "recognized" in caption:
        return caption["recognized"]
    return next((x[len(RECOGNIZED):] for x in caption.get("context", []) if x.startswith(RECOGNIZED)), "")


RECOGNIZED = "- Recognized in the photo: "
ALBUM_LINE = "- Album name (the owner's): \"{}\""


def run(cfg: Config, con, run_id: str):
    from .geo import Geocoder

    t0 = time.perf_counter()
    vlm = VLM(cfg.llm)
    model = f"{vlm.model}@{VERSION}"
    keep = {r[0] for r in con.execute("SELECT id FROM decision WHERE run=? AND verdict='keep'", (run_id,))}
    from .people import recognized

    names = recognized(con)
    done = {r[0]: json.loads(r[1]) for r in con.execute("SELECT id, data FROM caption WHERE model=?", (model,))}
    rows = [dict(r) for r in con.execute(
        "SELECT p.id, p.taken_local, p.lat, p.lon, MIN(f.album) AS album FROM photo p JOIN file f ON f.id = p.id"
        " WHERE f.policy NOT IN ('ignore', 'people', 'reference') GROUP BY p.id ORDER BY p.taken_ts")]
    try:  # an unsorted photo that joins an album (the `place` stage) is described with that album's context (P33)
        joins = {r["id"]: r["target"].removesuffix(" PRIV").removesuffix("/Dokumenty") for r in con.execute(
            "SELECT m.id, p.target FROM proposal p JOIN proposal_member m ON m.pid=p.pid WHERE p.run=? AND p.action='add'",
            (run_id,))}
        for row in rows:
            row["album"] = joins.get(row["id"], row["album"])
    except Exception:  # noqa: BLE001 - no place stage yet
        pass

    def stale(r):
        """Captioned again when who it shows (P40) or the album it's described with (P41) has changed, or it
        has no setting yet (P41)."""
        c = done.get(r["id"])
        if c is None or knew(c) != names.get(r["id"], "") or "setting" not in c:
            return True
        return bool(c.get("context")) and c["context"][0] != ALBUM_LINE.format(album_title(r["album"]))

    nude = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='nudity' AND value >= 1")}
    todo = [r for r in rows if (not keep or r["id"] in keep) and r["id"] not in nude and stale(r)]
    geo = Geocoder(cfg.models)
    trusted = {r["album"]: r["text"] for r in con.execute("SELECT album, text FROM album_context WHERE trusted=1")}
    mapper = TagMapper(cfg)
    siglip = SiglipCheck(cfg, mapper.paths, mapper.syn)
    sig = get_vecs(con, "siglip")
    for r in todo:
        dt = datetime.fromisoformat(r["taken_local"])
        ctx = [ALBUM_LINE.format(album_title(r["album"]))]
        g = None
        if r["lat"] is not None:
            g = geo.lookup(r["lat"], r["lon"])
            near = "in" if g["km"] < 2 else f"about {g['km']:.0f} km from"
            ctx.append(f"- Place: {near} {g['place']}, {g['region']}, {g['country']}")
        ctx.append(f"- When: a {dt.strftime('%A')} in {SEASONS[dt.month]} ({dt.strftime('%B')}), {part_of_day(dt.hour)}")
        if r["album"] in trusted:
            ctx.append(f"- The owner's notes for this album: {trusted[r['album']][:500]}")
        if names.get(r["id"]):
            ctx.append(RECOGNIZED + names[r["id"]])
        t1 = time.perf_counter()
        try:
            out = vlm.json([image_part(cfg.preview(r["id"])), {"type": "text", "text": INSTRUCTIONS.format(context="\n".join(ctx))}],
                           SCHEMA, SYSTEM)
        except (ValueError, KeyError) as exc:  # one bad answer skips that photo, not the stage
            print(f"captions: skipped {r['id'][:10]}: {exc}")
            continue
        out["context"], out["geo"], out["month"], out["recognized"] = ctx, g, dt.month, names.get(r["id"], "")
        con.execute("INSERT OR REPLACE INTO caption(id, model, data, seconds) VALUES (?,?,?,?)",
                    (r["id"], model, json.dumps(out, ensure_ascii=False), time.perf_counter() - t1))
        con.commit()

    # Tags: re-mapped for every caption on each run (cheap), so vocabulary edits apply without new VLM calls.
    month = {r["id"]: int(r["taken_local"][5:7]) for r in rows}
    needs_ocr = ocr_needed(con)
    for r in con.execute("SELECT id, data FROM caption WHERE model=?", (model,)).fetchall():
        if r["id"] not in month:  # captioned earlier, now only in a folder that isn't captioned (People)
            continue
        out = json.loads(r["data"])
        out.setdefault("month", month[r["id"]])
        shares = siglip.shares(sig[r["id"]]) if r["id"] in sig else {}
        mapped, review, names = [], [], []
        for tag, path, cos in mapper.map([t for t in out["tags"] if t.lower() not in STOP_TAGS]):
            if is_name(tag):
                names.append(tag)
            elif cos < MAP_MIN:
                review.append({"tag": tag, "nearest": path, "cos": round(cos, 3)})
            elif shares.get(path, 1.0) < SIGLIP_KEEP:
                review.append({"tag": tag, "nearest": path, "cos": round(cos, 3), "siglip_share": round(shares[path], 2),
                               "dropped": "SigLIP doesn't see it"})
            elif path not in [m["path"] for m in mapped]:
                mapped.append({"path": path, "pl": mapper.pl[path], "from": tag, "cos": round(cos, 3),
                               "siglip_share": round(shares.get(path, -1), 2)})
        if r["id"] in needs_ocr:  # a page of text the OCR post-process hasn't read yet
            mapped.append({"path": OCR_TAG[0], "pl": OCR_TAG[1], "from": "text stage"})
        season = f"Seasons/{SEASONS[out['month']].capitalize()}"
        if season not in [m["path"] for m in mapped]:
            mapped.append({"path": season, "pl": mapper.pl[season], "from": "capture date"})
        out["v2_tags"], out["tags_review"], out["names"] = mapped, review, names
        con.execute("UPDATE caption SET data=? WHERE id=? AND model=?", (json.dumps(out, ensure_ascii=False), r["id"], model))
    con.commit()

    # Pass 2 (text only): one summary per album from its captions. Written again only when its captions change,
    # and at temperature 0: the sort shows it next to the album, and a new wording each run moved a trip's
    # photos out of their album (P41, s6).
    albums = {}
    for r in con.execute("SELECT c.data, MIN(f.album) AS album FROM caption c JOIN file f ON f.id = c.id"
                         " WHERE c.model=? GROUP BY c.id ORDER BY c.id", (model,)):
        albums.setdefault(r["album"], []).append(json.loads(r["data"])["description_en"])
    had = {}
    for a, text in con.execute("SELECT album, text FROM album_context WHERE source=?", (f"v2 summary {VERSION}",)):
        try:
            had[a] = json.loads(text).get("of")
        except (ValueError, AttributeError):
            continue
    for album, descs in albums.items():
        of = hashlib.sha1("\n".join(descs).encode()).hexdigest()
        if had.get(album) == of:
            continue
        d = vlm._post({"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content":
            f'Album "{album_title(album)}". Captions of its photos:\n- ' + "\n- ".join(descs) +
            "\n\nWrite a 2-3 sentence summary of the album in Polish, then the same in English, as JSON with keys"
            ' "pl" and "en". Plain and concrete; name each kind of moment, place, person and animal the photos'
            " show, most common first."}], "max_tokens": 400, "temperature": 0,
            "response_format": {"type": "json_object"}})
        try:
            text = json.dumps({**json.loads(d["choices"][0]["message"]["content"]), "of": of}, ensure_ascii=False)
        except (ValueError, TypeError):
            continue
        con.execute("INSERT OR REPLACE INTO album_context(album, source, trusted, text) VALUES (?,?,0,?)",
                    (album, f"v2 summary {VERSION}", text))
    con.commit()
    dt_total = time.perf_counter() - t0
    log_timing(con, "captions", run_id, len(todo), dt_total, "4090 qwen3.8-27b")
    con.commit()
    print(f"captions: {len(todo)} photos, {vlm.stats()}, {dt_total:.0f} s")
