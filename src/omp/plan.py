"""The plan (S9, decisions.md P37): where every photo of the library ends up once the review is applied,
as a list of moves in the bundle (`OrderMyPhotos/plan.json`).

- A kept photo goes to the album or folder S11 proposed for it, or stays, or goes to its album's
  `Dokumenty/` (P24).
- An archived or bad one, and a copy of a photo that's already in an album, goes to
  `Archive/OrderMyPhotos/<its path>`.
- The reviewer's verdicts win over the pipeline's.
- A proposed turn (P36) is listed apart: on apply it's a lossless EXIF Orientation change, not a move.
- So are the tags that come from a detector rather than a caption: nudity and full nudity (P53, which
  replaced P26's move to the album's ` PRIV` sibling). They're written with the photo's other metadata (S9).
- `metadata.jsonl`, next to it: what goes into each kept photo's file, in one write (metadata.py, P67): the
  caption's title, description and tags, the named people and animals with their regions, the detectors' tags
  and the turn.
- Every move carries `id`, the QuickXorHash the photo must still have when it's moved (S9).
- `descriptions`: a kept photo's caption, Polish then English, for the OneDrive item description, which
  `mimir-onedrive apply` writes through Graph without a re-upload (S9, P56). The path is where the photo
  ends up.

Applying it is a move per photo, in place: nothing is copied (user, 2026-11-03: "do the moving in place as
not to extend disk space"). For a test, `python3 -m omp.plan --links <library> <out>` builds the resulting
folders as hardlinks next to the library (the same files, no extra space), so the review page, which shows
the photos where they are, keeps working.
"""

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import PurePosixPath

ARCHIVE = "Archive/OrderMyPhotos"


def run(cfg, con, run_id: str):
    dec = {r[0]: (r[1], json.loads(r[2] or "{}")) for r in con.execute(
        "SELECT id, verdict, features FROM decision WHERE run=?", (run_id,))}
    verdict = {}
    for r in con.execute("SELECT id, value FROM label WHERE kind='verdict' ORDER BY lid"):
        verdict[r[0]] = r[1]  # the reviewer's, the latest
    prop = {}
    for r in con.execute("SELECT m.id, p.action, p.target, p.data FROM proposal p JOIN proposal_member m ON m.pid=p.pid"
                         " WHERE p.run=?", (run_id,)):
        prop[r[0]] = (r[1], r[2], json.loads(r[3] or "{}").get("why", ""))
    from .nudity import TAGS

    described = {}  # photo id -> its caption's description, Polish then English (P42)
    for pid, data in con.execute("SELECT id, data FROM caption"):
        c = json.loads(data)
        texts = (str(c.get(k) or "").strip() for k in ("description_pl", "description_en"))
        described[pid] = "\n\n".join(t for t in texts if t)
    moves, rotations, tags, descriptions, counts, taken = [], [], [], [], Counter(), defaultdict(int)

    def to(folder, name):
        """A free name in the folder: a second photo with the same name gets _1, _2, ..."""
        stem, ext = os.path.splitext(name)
        k = taken[(folder, name.lower())]
        taken[(folder, name.lower())] += 1
        return f"{folder}/{stem}_{k}{ext}" if k else f"{folder}/{name}"

    kept = []  # the kept photos, for metadata.jsonl
    for path, id_, policy in con.execute("SELECT path, id, policy FROM file WHERE media='photo' ORDER BY path").fetchall():
        p = PurePosixPath(path)
        if policy in ("ignore", "people", "reference") or p.parts[0] == "OrderMyPhotos":
            continue
        v, f = dec.get(id_, ("keep", {}))
        v = verdict.get(id_, v)
        action, target, why = prop.get(id_, (None, None, ""))
        if v in ("archive", "bad") or (action == "copy" and policy == "unsorted"):
            dst, why = f"{ARCHIVE}/{path}", ("a copy of a photo in " + (target or "an album")) if action == "copy" else v
            if f.get("exact_copy") and v != "bad":
                why = "an exact copy of a photo that's kept"  # P54
            counts["archive"] += 1
        elif action in ("create", "add", "theme"):
            dst = to(target, p.name)
            counts[action] += 1
        elif f.get("place"):
            dst, why = to(f["place"], p.name), "its album's " + PurePosixPath(f["place"]).name
            counts["place"] += 1
        else:
            counts["stays"] += 1
            dst = None
        if dst and dst != path:
            moves.append({"from": path, "to": dst, "why": why, "id": id_})
        if described.get(id_) and v == "keep" and not (dst or "").startswith(ARCHIVE + "/"):
            descriptions.append({"path": dst or path, "id": id_, "text": described[id_]})
        if f.get("rotate") and v == "keep":
            rotations.append({"path": dst or path, "degrees": f["rotate"]})
        if f.get("nudity") in TAGS and v == "keep":
            tag, pl = TAGS[f["nudity"]]
            tags.append({"path": dst or path, "tags": [tag], "pl": [pl], "why": "nudity detector"})
        if v == "keep" and not (dst or path).startswith(ARCHIVE + "/"):
            kept.append({"id": id_, "path": path, "dst": dst or path, "turn": f.get("rotate") or 0,
                         "tags": [list(TAGS[f["nudity"]])] if f.get("nudity") in TAGS else []})
    from . import metadata  # noqa: PLC0415

    entries = metadata.plan(con, kept)
    with open(cfg.bundle / "metadata.jsonl", "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"plan: metadata for {len(entries)} of {len(kept)} kept photos -> {cfg.bundle / 'metadata.jsonl'}")
    out = cfg.bundle / "plan.json"
    out.write_text(json.dumps({"run": run_id, "generated": datetime.now().isoformat(timespec="seconds"),
                               "counts": dict(counts), "moves": moves, "rotations": rotations, "tags": tags,
                               "descriptions": descriptions}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"plan: {len(moves)} moves ({dict(counts)}), {len(rotations)} turns, {len(tags)} tagged,"
          f" {len(descriptions)} descriptions -> {out}")


def links(library: str, out: str) -> None:
    """The resulting folders as hardlinks: every file of the library at the place the plan gives it (the
    bundle left out). Same filesystem, no extra space; the library itself is untouched."""
    plan = json.load(open(os.path.join(library, "OrderMyPhotos", "plan.json"), encoding="utf-8"))
    moves = {m["from"]: m["to"] for m in plan["moves"]}
    n = moved = 0
    for dirpath, dirnames, files in os.walk(library):
        rel = os.path.relpath(dirpath, library)
        if rel.split(os.sep)[0] == "OrderMyPhotos":
            dirnames[:] = []
            continue
        for name in files:
            src_rel = PurePosixPath(rel, name).as_posix().removeprefix("./")
            dst_rel = moves.get(src_rel, src_rel)
            dst = os.path.join(out, dst_rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                os.link(os.path.join(dirpath, name), dst)
            n += 1
            moved += dst_rel != src_rel
    print(f"links: {n} files in {out}, {moved} at a new place")


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--links":
    links(sys.argv[2], sys.argv[3])
