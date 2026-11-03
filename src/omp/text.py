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
  text TEXT, ms REAL, created TEXT NOT NULL DEFAULT (datetime('now')),
  blocks TEXT,                         -- JSON layout blocks: type, level, box (0..1 of the page), text
  ocr_text TEXT,                       -- the OCR's Markdown before the repair pass
  repair TEXT                          -- JSON: model, changes kept and dropped by the guard
);
CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(id UNINDEXED, text);
"""

# The VLM tells reliably whether text is upright, but not which way a page is turned: on the sample it
# answered "90 clockwise" for a page that reads correctly only at 270 (the OCR came out as nonsense). So it
# is asked "upright?" about the photo as it is, and only if not, about each rotation; the best one wins.
UPRIGHT = ("Is the text in this photo upright, so that it can be read without turning the photo? "
           "Answer with one word: yes or no.")
TURNS = (90, 270, 180)
HAS_TEXT = ("Does this photo show text that someone would want to read: a page, document, receipt, label, sign, "
            "board or screen? Answer with one word: yes or no.")
MOSTLY_TEXT = ("Is this photo mostly a page of text to read: a document, a book or guide page, a letter, a receipt, "
               "or a board or screen full of text? Answer no if it shows people, a place or a thing that only has "
               "some text on it. Answer with one word: yes or no.")
OCR_TAG = ("Status/OCR needed", "Do OCR")  # the marker on a text page until the OCR post-process has read it


def plain_text(text: str) -> str:
    """The OCR writes tables (receipts, price lists) as HTML: one line per row, cells separated by tabs."""
    text = re.sub(r"</tr>\s*", "\n", text)
    text = re.sub(r"</t[dh]>\s*<t[dh][^>]*>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


# Unlimited-OCR-2 parses a page into layout blocks: <BLOCK>type<LEVELn>[x0, y0, x1, y1]<FORK>content, the
# box on a 0-999 grid of the page, in reading order. Kept as they are (with their positions) and turned
# into structured Markdown for the text file: headings by level, paragraphs, tables, figures and captions.
SPECIAL = re.compile(r"<｜[^｜]*｜>|<\|[^|]*\|>")
BLOCK = re.compile(r"<BLOCK>(\w+)(?:<LEVEL(\d+)>)?\[([\d,\s]*)\]<FORK>")


def blocks_of(raw: str) -> list[dict]:
    out = []
    for page_no, page in enumerate([p for p in SPECIAL.sub("", raw).split("<PAGE>") if p.strip()] or [""], 1):
        parts = BLOCK.split(page)
        for k in range(1, len(parts) - 3, 4):
            kind, level, box, content = parts[k], parts[k + 1], parts[k + 2], parts[k + 3].strip()
            nums = [int(n) for n in re.findall(r"\d+", box)]
            out.append({"page": page_no, "type": kind, "level": int(level) if level else None,
                        "box": [round(n / 999, 4) for n in nums[:4]] if len(nums) >= 4 else None, "text": content})
        if len(parts) == 1 and page.strip():
            out.append({"page": page_no, "type": "text", "level": None, "box": None, "text": page.strip()})
    return out


def table_markdown(table_html: str) -> str:
    rows = [[html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            for r in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S)]
    rows = [r for r in rows if r]
    if not rows:
        return plain_text(table_html)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def markdown_of(blocks: list[dict]) -> str:
    out, marks = [], []
    for b in blocks:
        kind, text = b["type"].lower(), b["text"]
        if kind in ("header", "footer", "page_number", "page_header", "page_footer"):
            if text:
                marks.append(text)  # running head and page number: one line on top, not in the text
        elif kind == "title" or kind.startswith("title"):
            out.append("#" * min((b["level"] or 1) + 1, 4) + " " + text)
        elif "table" in kind and "<t" in text:
            out.append(table_markdown(text))
        elif kind in ("image", "figure", "picture", "chart"):
            out.append(f"[{kind}{': ' + plain_text(text) if text else ''}]")
        elif "caption" in kind:
            out.append(f"*{plain_text(text)}*")
        elif kind in ("formula", "equation"):
            out.append(f"$$ {text} $$")
        elif text:
            out.append(plain_text(text))
    head = " · ".join(dict.fromkeys(marks))
    return ((f"_{head}_\n\n" if head else "") + "\n\n".join(out)).strip()


def candidates(con) -> list[str]:
    kinds = get_data(con, "kind")
    caps = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT id, data FROM caption")}
    out = []
    # Only photos that are reviewed or sorted: not a person's folder or a sample's thematic examples.
    for r in con.execute("SELECT DISTINCT s.id FROM signal s JOIN file f ON f.id=s.id WHERE s.kind='sharp'"
                         " AND f.policy NOT IN ('ignore', 'people', 'reference')"):
        i = r[0]
        top = [t["key"] for t in kinds.get(i, {}).get("top", [])][:2]
        c = caps.get(i, {})
        if (top and top[0] in TEXT_KINDS) or (len(top) > 1 and top[1] == "document" and top[0] in {"map", "accidental"}) \
                or c.get("category") in TEXT_CATEGORIES or len(c.get("text_in_image") or "") >= 120:
            out.append(i)
    return out


def flatten_page(img: Image.Image) -> Image.Image:
    """Find the page (the largest light four-sided shape) and warp it to a flat rectangle, so the OCR sees
    something like the scans it was trained on. If no clear page is found, the photo is used as it is."""
    import cv2
    import numpy as np

    a = np.asarray(img)
    s = 1000 / max(a.shape[:2])
    small = cv2.resize(a, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area = small.shape[0] * small.shape[1]
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        quad = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(quad) != 4 or cv2.contourArea(quad) < 0.25 * area or cv2.contourArea(quad) > 0.98 * area:
            continue
        pts = quad.reshape(4, 2).astype(np.float32) / s
        o = np.zeros((4, 2), np.float32)  # top-left, top-right, bottom-right, bottom-left
        o[0], o[2] = pts[np.argmin(pts.sum(1))], pts[np.argmax(pts.sum(1))]
        o[1], o[3] = pts[np.argmin(np.diff(pts, axis=1))], pts[np.argmax(np.diff(pts, axis=1))]
        w = int(max(np.linalg.norm(o[1] - o[0]), np.linalg.norm(o[2] - o[3])))
        h = int(max(np.linalg.norm(o[3] - o[0]), np.linalg.norm(o[2] - o[1])))
        m = cv2.getPerspectiveTransform(o, np.float32([[0, 0], [w, 0], [w, h], [0, h]]))
        return Image.fromarray(cv2.warpPerspective(a, m, (w, h), flags=cv2.INTER_CUBIC))
    return img


def upright(path: Path, rotate: int, flat: bool = False) -> bytes:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if rotate:
        img = img.rotate(-rotate, expand=True)  # PIL rotates counter-clockwise
    if flat:
        img = flatten_page(img)
    img.thumbnail((OCR_PX, OCR_PX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _schema(con):
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(text)")}
    for col in ("blocks", "ocr_text", "repair"):
        if col not in cols:
            con.execute(f"ALTER TABLE text ADD COLUMN {col} TEXT")


def ocr_needed(con) -> set[str]:
    """Text pages the OCR hasn't read with the block layout yet: they carry the "OCR needed" tag."""
    try:
        return {r[0] for r in con.execute("SELECT id FROM text WHERE mostly_text=1 AND blocks IS NULL")}
    except Exception:  # noqa: BLE001 - no text stage yet
        return set()


def run(cfg: Config, con, run_id: str):
    """S3c: which candidates are pages of text, and which way up, with two or three cheap questions to the
    resident chat VLM. A text page is tagged "OCR needed". The OCR itself is the `ocr` stage, a post-process
    once the albums are done and the photos sorted, for the pages that are kept (user, 2026-11-03)."""
    _schema(con)
    t0 = time.perf_counter()
    done = {r[0] for r in con.execute("SELECT id FROM text")}  # checked here, or read by an earlier OCR
    todo = [i for i in candidates(con) if i not in done]
    if not todo:
        return
    vlm = VLM(cfg.llm)
    model = f"{vlm.model}+check@1"

    def p_yes(img, question):
        ans = vlm.choice([image_part(img), {"type": "text", "text": question}], ["yes", "no"])
        return ans.get("yes", 0.5) if ans else 0.5

    n_none = n_pages = turned = 0
    for i in todo:
        img = Image.open(cfg.preview(i)).convert("RGB")
        # Asked "upright?", a photo without text gets a "no" and a rotation: so first, is there text at all?
        if p_yes(img, HAS_TEXT) < 0.4:
            n_none += 1
            con.execute("INSERT OR REPLACE INTO text(id, model, rotate, chars, mostly_text, text, blocks)"
                        " VALUES (?,?,0,0,0,'','[]')", (i, model))
            continue
        mostly = p_yes(img, MOSTLY_TEXT) >= 0.5
        rotate = 0
        if mostly and p_yes(img, UPRIGHT) < 0.5:
            scores = {deg: p_yes(img.rotate(-deg, expand=True), UPRIGHT) for deg in TURNS}
            best = max(scores, key=scores.get)
            rotate = best if scores[best] >= 0.5 else 0
        n_pages += mostly
        turned += bool(rotate)
        con.execute("INSERT OR REPLACE INTO text(id, model, rotate, chars, mostly_text) VALUES (?,?,?,NULL,?)",
                    (i, model, rotate, int(mostly)))
        con.commit()
    con.commit()
    t1 = time.perf_counter()
    log_timing(con, "text.check", run_id, len(todo), t1 - t0, "4090 qwen3.8-27b")
    con.commit()
    print(f"text: {len(todo)} candidates, {n_none} without text, {n_pages} pages of text (tagged OCR needed),"
          f" {turned} turned; {t1 - t0:.0f} s ({(t1 - t0) / len(todo):.1f} s/photo)")


def run_ocr(cfg: Config, con, run_id: str):
    """The OCR post-process: Unlimited-OCR-2 reads the kept text pages that still need it, keeping the block
    layout, then the chat VLM repairs OCR artifacts behind the guard (P28). Run it when the 4090 can be
    lent: the first request makes llama-swap load the OCR model and evict the chat model."""
    _schema(con)
    keep = {r[0] for r in con.execute("SELECT id FROM decision WHERE run=? AND verdict='keep'", (run_id,))}
    for r in con.execute("SELECT id, value FROM label WHERE kind='verdict' ORDER BY created"):
        (keep.add if r[1] == "keep" else keep.discard)(r[0])  # the reviewer's verdict wins
    rotate = {r[0]: r[1] for r in con.execute("SELECT id, rotate FROM text WHERE mostly_text=1 AND blocks IS NULL")
              if r[0] in keep}
    if not rotate:
        return
    path = {}
    for r in con.execute("SELECT id, path FROM file ORDER BY path"):
        path.setdefault(r["id"], cfg.root / r["path"])
    vlm = VLM(cfg.llm)
    t1 = time.perf_counter()
    url = f"{cfg.llm}/upstream/{MODEL}/v1/ocr"
    n_text, pages = 0, []
    with httpx.Client(timeout=1800) as client:
        for i in rotate:
            r = client.post(url, files=[("file", ("page.jpg", upright(path[i], rotate[i]), "image/jpeg"))])
            r.raise_for_status()
            out = r.json()
            blocks = blocks_of(out.get("raw") or out["text"])
            text = markdown_of(blocks) if blocks else plain_text(out["text"])
            n_text += len(text) >= MOSTLY_TEXT_CHARS
            pages.append(i)
            # It stays a text page: the VLM check said so, and a short page (a receipt, a label) is still one.
            con.execute("INSERT OR REPLACE INTO text(id, model, rotate, chars, mostly_text, text, ms, blocks, ocr_text)"
                        " VALUES (?,?,?,?,1,?,?,?,?)", (i, MODEL, rotate[i], len(text), text, out.get("ms"),
                                                       json.dumps(blocks, ensure_ascii=False), text))
            con.execute("DELETE FROM text_fts WHERE id=?", (i,))
            con.execute("INSERT INTO text_fts(id, text) VALUES (?,?)", (i, text))
            con.commit()
    t2 = time.perf_counter()

    # 3. Repair: the chat VLM fixes OCR artifacts, and the guard keeps only OCR-level corrections.
    n_kept = n_dropped = 0
    for i in pages:
        n_kept_i, n_dropped_i = repair_one(con, vlm, i)
        n_kept += n_kept_i
        n_dropped += n_dropped_i
    t3 = time.perf_counter()
    log_timing(con, "text.ocr", run_id, len(rotate), t2 - t1, "4090 unlimited-ocr-2")
    log_timing(con, "text.repair", run_id, len(pages), t3 - t2, "4090 qwen3.8-27b")
    con.commit()
    print(f"ocr: {len(rotate)} kept text pages read, {n_text} with {MOSTLY_TEXT_CHARS}+ characters;"
          f" OCR {t2 - t1:.0f} s ({(t2 - t1) / max(len(rotate), 1):.1f} s/page),"
          f" repair {t3 - t2:.0f} s: {n_kept} changes kept, {n_dropped} dropped by the guard")


def repair_one(con, vlm, i) -> tuple[int, int]:
    row = con.execute("SELECT ocr_text, text FROM text WHERE id=?", (i,)).fetchone()
    source = row["ocr_text"] or row["text"]
    fixed, kept, dropped = repair(vlm, source)
    con.execute("UPDATE text SET text=?, chars=?, repair=? WHERE id=?",
                (fixed, len(fixed), json.dumps({"model": vlm.model, "kept": kept, "dropped": dropped}, ensure_ascii=False), i))
    con.execute("DELETE FROM text_fts WHERE id=?", (i,))
    con.execute("INSERT INTO text_fts(id, text) VALUES (?,?)", (i, fixed))
    con.commit()
    return len(kept), len(dropped)


# --- Repair: an LLM pass over the OCR text, for OCR artifacts only (user, 2026-11-03) --------------------

REPAIR = """Below is OCR output from a photo of a printed page ({lang}). Fix only OCR errors:
- wrong or missing diacritics (e.g. "Kosčielec" -> "Kościelec", "Przętecz" -> "Przełęcz", "droge" -> "drogę");
- words split at line ends ("Gąsieni-cową" -> "Gąsienicową", "po-\nniżej" -> "poniżej"), but keep real
  hyphens ("pn.-zach.", "trawiasto-skaliste");
- broken spacing, stray line breaks inside a paragraph, and letters misread as similar ones.
Keep everything else exactly: the wording, abbreviations (pn., pd., zach., b., ok.), numbers, dates, names,
route letters and numbers, and the Markdown structure (headings, tables, emphasis). Do not add, drop,
translate or explain anything. Output only the corrected text.

OCR output:
{text}"""
REPAIR_CHUNK = 3000   # characters per call, split at paragraphs


def repair(vlm, text: str, lang: str = "Polish") -> tuple[str, list, list]:
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) > REPAIR_CHUNK:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    out, kept, dropped = [], [], []
    for chunk in chunks:
        d = vlm._post({"messages": [{"role": "user", "content": REPAIR.format(lang=lang, text=chunk)}],
                       "max_tokens": len(chunk) // 2 + 400, "temperature": 0})
        merged, k, dr = safe_merge(chunk, d["choices"][0]["message"]["content"].strip())
        out.append(merged)
        kept += k
        dropped += dr
    return "\n\n".join(out), kept, dropped


# The guard: the LLM's changes are kept only where they are OCR-level corrections. On the sample, Qwen3.8 got
# ~70 % of its changes right (diacritics, joined words) but ~25 % wrong, some changing the meaning ("zlebu" ->
# "zlewu", "Myna" -> "Młynem", "Przętecz" -> "Przełącz"). A change passes only if the word keeps its letters
# and length and differs by diacritics or by the OCR's typical confusions, or if it joins a word split at a
# line end. Everything else keeps the OCR's version.
_SAME = {("a", "ą"), ("c", "ć"), ("e", "ę"), ("l", "ł"), ("n", "ń"), ("o", "ó"), ("s", "ś"), ("z", "ź"), ("z", "ż"),
         ("t", "ł"), ("č", "ś"), ("č", "ć"), ("š", "ś"), ("ž", "ż"), ("ě", "ę"), ("ĺ", "ł"), ("ŕ", "ś"), ("ą", "ę"),
         ("ę", "ą"), ("ó", "o"), ("ł", "l")}


def _letters_ok(old: str, new: str) -> bool:
    if len(old) != len(new):
        return False
    for a, b in zip(old, new):
        al, bl = a.lower(), b.lower()
        if al != bl and (al, bl) not in _SAME and (bl, al) not in _SAME:
            return False
    return True


def _pair_ok(old: str, new: str) -> bool:
    return _letters_ok(old, new) or ("-" in old and _letters_ok(old.replace("-", ""), new))


def safe_merge(ocr: str, fixed: str) -> tuple[str, list, list]:
    """Apply only the safe word changes of `fixed` to `ocr`, keeping the OCR's spacing and line breaks."""
    import difflib

    toks = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\S+", ocr)]
    new = fixed.split()
    edits, kept, dropped = [], [], []   # (first token, last token, replacement)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [t[2] for t in toks], new, autojunk=False).get_opcodes():
        if op == "equal" or i1 == i2 or j1 == j2:
            if op != "equal":
                dropped.append((" ".join(t[2] for t in toks[i1:i2]), " ".join(new[j1:j2])))
            continue
        i, j = i1, j1
        while i < i2 and j < j2:
            a, b = toks[i][2], new[j]
            if _pair_ok(a, b):
                if a != b:
                    edits.append((i, i, b))
                    kept.append((a, b))
                i, j = i + 1, j + 1
            elif i + 1 < i2 and _pair_ok(a.rstrip("-") + toks[i + 1][2], b):  # a word split at a line end
                edits.append((i, i + 1, b))
                kept.append((a + " " + toks[i + 1][2], b))
                i, j = i + 2, j + 1
            else:
                dropped.append((a, b))
                i, j = i + 1, j + 1
        for k in range(i, i2):
            dropped.append((toks[k][2], ""))
    out, pos = [], 0
    for first, last, rep in edits:
        out.append(ocr[pos:toks[first][0]])
        out.append(rep)
        pos = toks[last][1]
    out.append(ocr[pos:])
    return "".join(out), kept, dropped
