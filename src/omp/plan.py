"""The plan (S9, decisions.md P37): where every photo of the library ends up once the review is applied,
as a list of moves in the bundle (`OrderMyPhotos/plan.json`).

- A kept photo goes to the album or folder S11 proposed for it, or stays, or goes to its album's
  `Dokumenty/` or ` PRIV` sibling (P24, P26).
- An archived or bad one, and a copy of a photo that's already in an album, goes to
  `Archive/OrderMyPhotos/<its path>`.
- The reviewer's verdicts win over the pipeline's.
- A proposed turn (P36) is listed apart: on apply it's a lossless EXIF Orientation change, not a move.

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
    moves, rotations, counts, taken = [], [], Counter(), defaultdict(int)

    def to(folder, name):
        """A free name in the folder: a second photo with the same name gets _1, _2, ..."""
        stem, ext = os.path.splitext(name)
        k = taken[(folder, name.lower())]
        taken[(folder, name.lower())] += 1
        return f"{folder}/{stem}_{k}{ext}" if k else f"{folder}/{name}"

    for path, id_, policy in con.execute("SELECT path, id, policy FROM file WHERE media='photo' ORDER BY path").fetchall():
        p = PurePosixPath(path)
        if policy in ("ignore", "people", "reference") or p.parts[0] == "OrderMyPhotos":
            continue
        v, f = dec.get(id_, ("keep", {}))
        v = verdict.get(id_, v)
        action, target, why = prop.get(id_, (None, None, ""))
        if v in ("archive", "bad") or (action == "copy" and policy == "unsorted"):
            dst, why = f"{ARCHIVE}/{path}", ("a copy of a photo in " + (target or "an album")) if action == "copy" else v
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
            moves.append({"from": path, "to": dst, "why": why})
        if f.get("rotate") and v == "keep":
            rotations.append({"path": dst or path, "degrees": f["rotate"]})
    out = cfg.bundle / "plan.json"
    out.write_text(json.dumps({"run": run_id, "generated": datetime.now().isoformat(timespec="seconds"),
                               "counts": dict(counts), "moves": moves, "rotations": rotations},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"plan: {len(moves)} moves ({dict(counts)}), {len(rotations)} turns -> {out}")


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
