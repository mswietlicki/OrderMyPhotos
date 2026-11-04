"""Applying the plan (S9, decisions.md P48) to a session's own copy of photos: a test set, which is a copy of
some OneDrive photos. OneDrive itself is never touched here; the library's plan goes through `mimir-onedrive
apply` (server-side moves by item id), which is still to build.

- Every move of `plan.json` is done in place, as a rename: nothing is copied. Before a photo moves, its
  QuickXorHash is checked against the DB, so a file that changed since the run isn't moved. A destination
  that exists already gets `_1`, `_2`, …, never overwritten.
- The turns (P36) are written as the EXIF Orientation tag, a lossless change, on the moved photo.
- If the final decision is to delete the archived photos, the photos this apply moved into
  `Archive/OrderMyPhotos/` are deleted afterwards, and only those.
- Each step is a line in `<session>/apply.jsonl`, so what happened can be read back and the moves undone.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from .qxh import quickxorhash_file

ARCHIVE = "Archive/OrderMyPhotos"
# Where a session's copy may be changed: the test sets. Never the OneDrive mirror or its snapshots.
APPLY_ROOTS = [Path(p) for p in os.environ.get("OMP_APPLY_ROOTS", "/srv/mimir/cache/photos/tests").split(":") if p]
NEVER = Path("/srv/mimir/onedrive")
TURN = [1, 6, 3, 8]  # EXIF Orientation of an unmirrored photo, a quarter turn clockwise at each step


def appliable(root: Path) -> tuple[bool, str]:
    """Whether a session's plan may be applied here, and why not."""
    r = root.resolve()
    if r == NEVER.resolve() or r.is_relative_to(NEVER.resolve()):
        return False, "this is the OneDrive library: its plan is applied on OneDrive by mimir-onedrive (not built yet)"
    if not any(r.is_relative_to(b.resolve()) for b in APPLY_ROOTS):
        return False, f"only copies in {', '.join(map(str, APPLY_ROOTS))} are changed here"
    return True, ""


def free(path: Path) -> Path:
    """The path, or the first `name_1.ext`, `name_2.ext` … that doesn't exist yet."""
    n, stem, ext = 0, path.stem, path.suffix
    while path.exists():
        n += 1
        path = path.with_name(f"{stem}_{n}{ext}")
    return path


def orientation(path: Path) -> int | None:
    out = subprocess.run(["exiftool", "-n", "-s3", "-Orientation", str(path)], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def set_orientation(path: Path, value: int) -> bool:
    res = subprocess.run(["exiftool", "-n", f"-Orientation={value}", "-overwrite_original", "-q", str(path)], capture_output=True, text=True)
    return res.returncode == 0


def turn(path: Path, degrees: int) -> tuple[int | None, int | None]:
    """A clockwise turn written as the EXIF Orientation: lossless, the pixels stay as they are. Returns the
    orientation before and after (after None: not turned)."""
    now = orientation(path) or 1
    if now not in TURN:  # mirrored: left as it is
        return now, None
    new = TURN[(TURN.index(now) + degrees // 90) % 4]
    return now, new if set_orientation(path, new) else None


def run(cfg, con, run_id: str):
    from . import runtime  # noqa: PLC0415

    root = Path(cfg.root)
    ok, why = appliable(root)
    if not ok:
        raise SystemExit(f"apply: {why}")
    plan = json.loads((cfg.bundle / "plan.json").read_text(encoding="utf-8"))
    state = Path(cfg.db).parent
    final = json.loads((state / "final.json").read_text(encoding="utf-8"))
    hashes = {r[0]: r[1] for r in con.execute("SELECT path, id FROM file")}
    ours = {}  # photo id -> hashes this session's own turns and undos gave it
    if (state / "apply.jsonl").exists():
        for line in open(state / "apply.jsonl", encoding="utf-8"):
            e = json.loads(line)
            if e.get("id") and e.get("hash"):
                ours.setdefault(e["id"], set()).add(e["hash"])
    log = open(state / "apply.jsonl", "a", encoding="utf-8")

    def note(**entry):
        log.write(json.dumps(entry | {"t": time.time()}, ensure_ascii=False) + "\n")
        log.flush()

    note(op="start", plan=plan.get("generated"), archived=final.get("archived"), moves=len(plan["moves"]))
    moved, skipped, archived, placed = 0, 0, [], {}
    with runtime.Progress(con, "apply.moves", "plan", total=len(plan["moves"])) as prog:
        for m in plan["moves"]:
            src, dst = root / m["from"], free(root / m["to"])
            if not src.is_file():
                prog.fail(m["from"], "not there any more")
                note(op="skip", path=m["from"], why="missing")
                skipped += 1
                continue
            h, want = quickxorhash_file(src).hex(), hashes.get(m["from"])
            if want and h != want and h not in ours.get(want, ()):
                prog.fail(m["from"], "changed since the run")
                note(op="skip", path=m["from"], why="changed")
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            rel = dst.relative_to(root).as_posix()
            placed[m["to"]] = (rel, want)
            note(op="move", src=m["from"], dst=rel, id=want, why=m.get("why"))
            if rel.startswith(ARCHIVE + "/"):
                archived.append(rel)
            moved += 1
            prog.done(m["from"])
    turned = 0
    for r in plan.get("rotations", []):
        rel, id_ = placed.get(r["path"], (r["path"], hashes.get(r["path"])))
        p = root / rel
        if p.is_file():
            before, after = turn(p, r["degrees"])
            note(op="turn", path=rel, degrees=r["degrees"], before=before, after=after, id=id_,
                 hash=quickxorhash_file(p).hex() if after else None)
            turned += after is not None
    deleted = 0
    if final.get("archived") == "delete":
        for rel in archived:  # only what this apply archived, nothing else in the archive
            (root / rel).unlink(missing_ok=True)
            note(op="delete", path=rel)
            deleted += 1
        # the archive's emptied folders, deepest first
        for d in sorted({(root / rel).parent for rel in archived}, key=lambda x: -len(x.parts)):
            while d != root and d.is_dir() and not any(d.iterdir()) and (root / ARCHIVE) in [d, *d.parents]:
                d.rmdir()
                d = d.parent
    # folders the moves left empty
    for d in sorted({(root / m["from"]).parent for m in plan["moves"]}, key=lambda x: -len(x.parts)):
        while d != root and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
    note(op="end", moved=moved, skipped=skipped, turned=turned, deleted=deleted)
    log.close()
    print(f"apply: {moved} photos moved ({len(archived)} to {ARCHIVE}), {skipped} skipped, {turned} turned,"
          f" {deleted} archived photos deleted, in {root}")


def undo(cfg):
    """The moves of the last apply, back where they were (deleted photos can't come back)."""
    state = Path(cfg.db).parent
    entries = [json.loads(line) for line in open(state / "apply.jsonl", encoding="utf-8")]
    last = max(i for i, e in enumerate(entries) if e["op"] == "start")
    gone = {e["path"] for e in entries[last:] if e["op"] == "delete"}
    log = open(state / "apply.jsonl", "a", encoding="utf-8")
    for e in entries[last:]:  # the turns first, while the photos are where the turns found them
        p = Path(cfg.root) / e.get("path", "")
        if e["op"] == "turn" and e.get("after") and e["path"] not in gone and p.is_file() and set_orientation(p, e["before"]):
            log.write(json.dumps({"op": "unturn", "path": e["path"], "id": e.get("id"), "hash": quickxorhash_file(p).hex(),
                                  "t": time.time()}) + "\n")
    log.close()
    back = 0
    for e in reversed(entries[last:]):
        if e["op"] == "move" and e["dst"] not in gone and (Path(cfg.root) / e["dst"]).is_file():
            src = Path(cfg.root) / e["src"]
            src.parent.mkdir(parents=True, exist_ok=True)
            os.rename(Path(cfg.root) / e["dst"], free(src))
            back += 1
    print(f"apply --undo: {back} photos back where they were, {len(gone)} were deleted")
