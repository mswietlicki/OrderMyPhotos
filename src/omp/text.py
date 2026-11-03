"""Text in photos: OCR for photos of documents, book pages, notes, receipts and labels (design.md S3b/S7).

1. Candidates: photos the zero-shot kinds or the captions call text-like.
2. Orientation: the local VLM says which way the page is turned. A turned page gets a lossless rotate
   proposal (EXIF Orientation), and the OCR reads it upright.
3. OCR: Unlimited-OCR-2 on the RTX 4090 (mimir stacks/ocr, started by llama-swap, which evicts the chat
   model while it runs), on the original at up to 2560 px.
4. The text goes into the DB with an FTS5 index. A photo that is mostly text gets a `.txt` file of the
   same name next to it when the plan is applied, and moves into the album's `Dokumenty/` folder.
"""

import html
import io
import json
import re
import time
from pathlib import Path

import httpx
from PIL import Image, ImageOps

from .config import Config
from .db import get_data, log_timing
from .vlm import VLM, image_part

MODEL = "unlimited-ocr-2"
TEXT_KINDS = {"document", "receipt", "label", "screen", "whiteboard"}
TEXT_CATEGORIES = {"documents", "receipts", "labels", "specs", "screens", "to_remember"}
MOSTLY_TEXT_CHARS = 300   # cleaned OCR text this long, on a document-like photo, makes it a text page
OCR_PX = 2560
DOCS_FOLDER = "Dokumenty"  # where an album's text pages go (user, 2026-11-03); configurable

SCHEMA = """
CREATE TABLE IF NOT EXISTS text (
  id TEXT PRIMARY KEY, model TEXT NOT NULL,
  rotate INTEGER NOT NULL DEFAULT 0,   -- degrees clockwise that make the page upright
  chars INTEGER, mostly_text INTEGER NOT NULL DEFAULT 0,
  text TEXT, ms REAL, created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(id UNINDEXED, text);
"""

# The VLM tells reliably whether text is upright, but not which way a page is turned: on the sample it
# answered "90 clockwise" for a page that reads correctly only at 270 (the OCR came out as nonsense). So it
# is asked "upright?" about the photo as it is, and only if not, about each rotation; the best one wins.
UPRIGHT = ("Is the text in this photo upright, so that it can be read without turning the photo? "
           "Answer with one word: yes or no.")
TURNS = (90, 270, 180)


def plain_text(text: str) -> str:
    """The OCR writes tables (receipts, price lists) as HTML: one line per row, cells separated by tabs."""
    text = re.sub(r"</tr>\s*", "\n", text)
    text = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def candidates(con) -> list[str]:
    kinds = get_data(con, "kind")
    caps = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT id, data FROM caption")}
    out = []
    for r in con.execute("SELECT DISTINCT id FROM signal WHERE kind='sharp'"):
        i = r[0]
        top = [t["key"] for t in kinds.get(i, {}).get("top", [])][:2]
        c = caps.get(i, {})
        if (top and top[0] in TEXT_KINDS) or (len(top) > 1 and top[1] == "document" and top[0] in {"map", "accidental"}) \
                or c.get("category") in TEXT_CATEGORIES or len(c.get("text_in_image") or "") >= 120:
            out.append(i)
    return out


def upright(path: Path, rotate: int) -> bytes:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if rotate:
        img = img.rotate(-rotate, expand=True)  # PIL rotates counter-clockwise
    img.thumbnail((OCR_PX, OCR_PX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def run(cfg: Config, con, run_id: str):
    con.executescript(SCHEMA)
    t0 = time.perf_counter()
    done = {r[0] for r in con.execute("SELECT id FROM text WHERE model=?", (MODEL,))}
    todo = [i for i in candidates(con) if i not in done]
    if not todo:
        return
    path = {}
    for r in con.execute("SELECT id, path FROM file ORDER BY path"):
        path.setdefault(r["id"], cfg.root / r["path"])

    # 1. Orientation, with the resident chat VLM, before the OCR model takes the 4090 over.
    vlm = VLM(cfg.llm)
    def p_upright(img):
        ans = vlm.choice([image_part(img), {"type": "text", "text": UPRIGHT}], ["yes", "no"])
        return ans.get("yes", 0.5) if ans else 0.5

    rotate = {}
    for i in todo:
        img = Image.open(cfg.preview(i)).convert("RGB")
        if p_upright(img) >= 0.5:
            rotate[i] = 0
            continue
        scores = {deg: p_upright(img.rotate(-deg, expand=True)) for deg in TURNS}
        best = max(scores, key=scores.get)
        rotate[i] = best if scores[best] >= 0.5 else 0
    t1 = time.perf_counter()

    # 2. OCR. The first request makes llama-swap load the OCR model (and evict the chat model).
    url = f"{cfg.llm}/upstream/{MODEL}/v1/ocr"
    n_text = 0
    with httpx.Client(timeout=1800) as client:
        for i in todo:
            r = client.post(url, files=[("file", ("page.jpg", upright(path[i], rotate[i]), "image/jpeg"))])
            r.raise_for_status()
            out = r.json()
            text = plain_text(out["text"])
            mostly = int(len(text) >= MOSTLY_TEXT_CHARS)
            n_text += mostly
            con.execute("INSERT OR REPLACE INTO text(id, model, rotate, chars, mostly_text, text, ms) VALUES (?,?,?,?,?,?,?)",
                        (i, MODEL, rotate[i], len(text), mostly, text, out.get("ms")))
            con.execute("DELETE FROM text_fts WHERE id=?", (i,))
            con.execute("INSERT INTO text_fts(id, text) VALUES (?,?)", (i, text))
            con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "text.orientation", run_id, len(todo), t1 - t0, "4090 qwen3.8-27b")
    log_timing(con, "text.ocr", run_id, len(todo), dt - (t1 - t0), "4090 unlimited-ocr-2")
    con.commit()
    turned = sum(1 for v in rotate.values() if v)
    print(f"text: {len(todo)} candidates, {turned} turned, {n_text} mostly text; orientation {t1 - t0:.0f} s,"
          f" OCR {dt - (t1 - t0):.0f} s ({(dt - (t1 - t0)) / len(todo):.1f} s/page)")
