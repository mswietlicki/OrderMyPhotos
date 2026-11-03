"""S6 (minimal): a static review page for one run, used to label the sample.

It's a self-contained folder (index.html + thumbnails + previews, nothing loaded from the internet),
opened locally in a browser. Labels are kept in the browser's localStorage and exported as JSON, then
imported with `omp labels --file labels.json`; they are the ground truth the thresholds are tuned on.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath

from .config import Config


def run(cfg: Config, con, run_id: str, out: str | None):
    out_dir = Path(out) if out else cfg.cache / "report" / run_id
    (out_dir / "t").mkdir(parents=True, exist_ok=True)
    (out_dir / "p").mkdir(parents=True, exist_ok=True)

    files = {}
    for r in con.execute("SELECT id, path, v1_archived, album FROM file ORDER BY path"):
        files.setdefault(r["id"], dict(r))
    photos = {r["id"]: dict(r) for r in con.execute("SELECT id, taken_local, taken_ts, lat FROM photo")}
    dec = {r["id"]: dict(r) for r in con.execute("SELECT * FROM decision WHERE run=?", (run_id,))}
    caps = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT id, data FROM caption")}
    groups = {}
    for g in con.execute("SELECT gid, kind, cohesion, data FROM grp WHERE run=?", (run_id,)):
        groups[g["gid"]] = {"kind": g["kind"], "cohesion": g["cohesion"], **json.loads(g["data"] or "{}"),
                            "members": [m[0] for m in con.execute("SELECT id FROM grp_member WHERE gid=? ORDER BY ord",
                                                                  (g["gid"],))]}
    event_of = {i: g["index"] for g in groups.values() if g["kind"] == "event" for i in g["members"]}
    v1 = {}
    for r in con.execute("SELECT album, first_file, kept, archived FROM v1_burst"):
        for f in json.loads(r["kept"]):
            v1[f] = ("kept", r["first_file"])
        for f in json.loads(r["archived"]):
            v1[f] = ("archived", r["first_file"])

    items = []
    for id_, f in files.items():
        if id_ not in dec:
            continue
        d, p = dec[id_], photos[id_]
        name = PurePosixPath(f["path"]).name
        shutil.copyfile(cfg.thumb(id_), out_dir / "t" / f"{id_}.jpg")
        shutil.copyfile(cfg.preview(id_), out_dir / "p" / f"{id_}.jpg")
        feats = json.loads(d["features"])
        c = caps.get(id_)
        items.append({
            "id": id_, "name": name, "time": p["taken_local"][11:19], "date": p["taken_local"][:10], "ts": p["taken_ts"],
            "event": event_of.get(id_, 0), "group": f"B{d['gid']}" if d["gid"] is not None else "",
            "verdict": d["verdict"], "tier": d["tier"], "rank": d["rank"], "score": round(d["score"], 3),
            "reasons": json.loads(d["reasons"]), "floor": json.loads(d["floor"]) if d["floor"] else None,
            "protected": d["protected"],
            "f": {k: feats.get(k) for k in ("sharp", "rel_sharp", "quality", "aesthetics", "motion", "faces", "purpose",
                                           "memory_p", "bt", "p_keep", "clip_hi")},
            "v1": (v1.get(name) or (None,))[0], "v1_archived": bool(f["v1_archived"]),
            "cap": None if not c else {k: c.get(k) for k in ("title_pl", "title_en", "description_pl", "description_en",
                                                             "text_in_image", "purpose", "category", "names")}
                   | {"tags": [t["path"] for t in c.get("v2_tags", [])]},
        })
    items.sort(key=lambda x: (x["ts"], x["name"]))
    bursts = {f"B{gid}": {"cohesion": g["cohesion"], "span": g.get("span_s"), "min": g.get("min_margin")}
              for gid, g in groups.items() if g["kind"] == "burst"}
    album = next(iter(files.values()))["album"]
    summary = con.execute("SELECT text FROM album_context WHERE album=? AND source LIKE 'v2 summary%'", (album,)).fetchone()
    timings = [dict(r) for r in con.execute("SELECT stage, items, round(seconds, 1) AS s, device FROM timing"
                                            " WHERE rowid IN (SELECT MAX(rowid) FROM timing GROUP BY stage) ORDER BY rowid")]
    data = {"run": run_id, "album": album, "generated": datetime.now().isoformat(timespec="seconds"),
            "items": items, "bursts": bursts, "summary": json.loads(summary[0]) if summary else None, "timings": timings}
    html = (Path(__file__).parent / "report.html").read_text(encoding="utf-8")
    (out_dir / "index.html").write_text(html.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False)),
                                        encoding="utf-8")
    print(f"report: {len(items)} photos -> {out_dir}/index.html")


def import_labels(cfg: Config, con, path: str):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    by_name = {PurePosixPath(r["path"]).name: r["id"] for r in con.execute("SELECT id, path FROM file")}
    n = 0
    src = f"user:{spec.get('run')}:{spec.get('exported')}"
    for name, lab in spec["labels"].items():
        if not lab.get("confirmed") or name not in by_name:
            continue
        id_ = by_name[name]
        for kind in ("group", "verdict", "category", "note"):
            if lab.get(kind) not in (None, ""):
                con.execute("INSERT INTO label(id, kind, value, source) VALUES (?,?,?,?)", (id_, kind, str(lab[kind]), src))
        n += 1
    con.commit()
    print(f"labels: {n} confirmed photos imported from {path}")
