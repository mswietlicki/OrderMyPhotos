"""Measure a run against the user's labels (design.md, Evaluation): bursts as pairwise precision/recall,
split by time gap, and picks as the false-archive rate (photos the user keeps that the run archives)."""

import json
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import PurePosixPath

LONG_GAP = 60  # seconds: short and long bursts are reported apart


def latest_labels(con) -> dict[str, dict]:
    out = defaultdict(dict)
    for r in con.execute("SELECT id, kind, value FROM label ORDER BY lid"):
        out[r["id"]][r["kind"]] = r["value"]
    return dict(out)


def run(cfg, con, run_id: str) -> dict:
    labels = latest_labels(con)
    dec = {r["id"]: dict(r) for r in con.execute("SELECT * FROM decision WHERE run=?", (run_id,))}
    ts = {r["id"]: r["taken_ts"] for r in con.execute("SELECT id, taken_ts FROM photo")}
    name = {}
    for r in con.execute("SELECT id, path FROM file ORDER BY path"):
        name.setdefault(r["id"], PurePosixPath(r["path"]).name)
    ids = sorted((i for i in dec if i in labels and "verdict" in labels[i]), key=lambda i: ts[i])
    if not ids:
        print("evaluate: no labels for this run's photos")
        return {}

    # Bursts: the user's group labels ("" = single); a pipeline group is the decision's gid.
    def pairs(group_of):
        out = set()
        by = defaultdict(list)
        for i in ids:
            if group_of(i):
                by[group_of(i)].append(i)
        for members in by.values():
            out |= {tuple(sorted(p)) for p in combinations(members, 2)}
        return out

    truth = pairs(lambda i: labels[i].get("group", ""))
    pred = pairs(lambda i: dec[i]["gid"])
    res = {"photos": len(ids)}
    for label, sel in (("short", lambda p: abs(ts[p[0]] - ts[p[1]]) < LONG_GAP),
                       ("long", lambda p: abs(ts[p[0]] - ts[p[1]]) >= LONG_GAP), ("all", lambda p: True)):
        t, p = {x for x in truth if sel(x)}, {x for x in pred if sel(x)}
        tp = len(t & p)
        res[f"pairs_{label}"] = {"truth": len(t), "pred": len(p), "tp": tp,
                                 "precision": round(tp / len(p), 3) if p else None,
                                 "recall": round(tp / len(t), 3) if t else None}
    res["wrong_pairs"] = sorted(f"{name[a][9:15]}~{name[b][9:15]}" for a, b in pred - truth)
    res["missed_pairs"] = sorted(f"{name[a][9:15]}~{name[b][9:15]}" for a, b in truth - pred)

    # Picks. The user's "bad" counts as archive; "keep" vs "archive" against the run's verdict.
    user = {i: ("archive" if labels[i]["verdict"] == "bad" else labels[i]["verdict"]) for i in ids}
    conf = Counter((user[i], dec[i]["verdict"]) for i in ids)
    kept = [i for i in ids if user[i] == "keep"]
    false_archive = [i for i in kept if dec[i]["verdict"] == "archive"]
    false_keep = [i for i in ids if user[i] == "archive" and dec[i]["verdict"] == "keep"]
    res["confusion"] = {f"user {u} / run {v}": n for (u, v), n in sorted(conf.items())}
    res["false_archive_rate"] = round(len(false_archive) / len(kept), 3) if kept else None
    res["false_archives"] = [name[i][9:15] for i in false_archive]
    res["false_keeps"] = [name[i][9:15] for i in false_keep]

    # Top-1: in each burst the user labelled (2+ photos), is the run's best frame one the user keeps?
    by = defaultdict(list)
    for i in ids:
        if labels[i].get("group"):
            by[labels[i]["group"]].append(i)
    hits = total = 0
    keep_counts = []
    for g, members in by.items():
        if len(members) < 2:
            continue
        best = min(members, key=lambda i: (dec[i]["gid"] is None, dec[i]["rank"]))
        total += 1
        hits += user[best] == "keep"
        keep_counts.append((sum(user[i] == "keep" for i in members), sum(dec[i]["verdict"] == "keep" for i in members),
                            len(members)))
    res["top1"] = f"{hits}/{total}"
    res["kept_per_burst"] = {"user": sum(k[0] for k in keep_counts), "run": sum(k[1] for k in keep_counts),
                             "photos": sum(k[2] for k in keep_counts)}
    res["singles_bad"] = sum(1 for i in ids if not labels[i].get("group") and labels[i]["verdict"] == "bad")
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return res
