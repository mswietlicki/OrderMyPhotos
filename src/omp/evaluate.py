"""Measure a run against the user's labels (design.md, Evaluation): bursts as pairwise precision/recall,
split by time gap, and picks as the false-archive rate (photos the user keeps that the run archives).
Reported for all labelled photos and per album."""

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


def metrics(ids, labels, dec, ts, name) -> dict:
    def pairs(group_of):
        by = defaultdict(list)
        for i in ids:
            if group_of(i):
                by[group_of(i)].append(i)
        return {tuple(sorted(p)) for members in by.values() for p in combinations(members, 2)}

    truth = pairs(lambda i: labels[i].get("group", ""))
    pred = pairs(lambda i: dec[i]["gid"])
    res = {"photos": len(ids)}
    for label, sel in (("short", lambda p: abs(ts[p[0]] - ts[p[1]]) < LONG_GAP),
                       ("long", lambda p: abs(ts[p[0]] - ts[p[1]]) >= LONG_GAP)):
        t, p = {x for x in truth if sel(x)}, {x for x in pred if sel(x)}
        tp = len(t & p)
        res[f"pairs_{label}"] = {"truth": len(t), "pred": len(p), "tp": tp,
                                 "precision": round(tp / len(p), 3) if p else None,
                                 "recall": round(tp / len(t), 3) if t else None}
    res["wrong_pairs"] = len(pred - truth)
    res["missed_pairs"] = len(truth - pred)

    user = {i: ("archive" if labels[i]["verdict"] == "bad" else labels[i]["verdict"]) for i in ids}
    kept = [i for i in ids if user[i] == "keep"]
    archived = [i for i in ids if user[i] == "archive"]
    false_archive = [i for i in kept if dec[i]["verdict"] == "archive"]
    false_keep = [i for i in archived if dec[i]["verdict"] == "keep"]
    res["user_keep"], res["run_keep"] = len(kept), sum(1 for i in ids if dec[i]["verdict"] == "keep")
    res["false_archive_rate"] = round(len(false_archive) / len(kept), 3) if kept else None
    res["false_keep_rate"] = round(len(false_keep) / len(archived), 3) if archived else None
    res["false_archives"] = [name[i] for i in false_archive]
    res["false_keeps"] = [name[i] for i in false_keep]

    by = defaultdict(list)
    for i in ids:
        if labels[i].get("group"):
            by[labels[i]["group"]].append(i)
    hits = total = 0
    for members in by.values():
        if len(members) < 2:
            continue
        best = min(members, key=lambda i: (dec[i]["gid"] is None, dec[i]["rank"]))
        total += 1
        hits += user[best] == "keep"
    res["top1"] = f"{hits}/{total}"
    return res


def run(cfg, con, run_id: str, verbose=False) -> dict:
    labels = latest_labels(con)
    dec = {r["id"]: dict(r) for r in con.execute("SELECT * FROM decision WHERE run=?", (run_id,))}
    ts = {r["id"]: r["taken_ts"] for r in con.execute("SELECT id, taken_ts FROM photo")}
    name, album = {}, {}
    for r in con.execute("SELECT id, path, album FROM file ORDER BY path"):
        name.setdefault(r["id"], PurePosixPath(r["path"]).name)
        album.setdefault(r["id"], r["album"])
    ids = sorted((i for i in dec if i in labels and "verdict" in labels[i]), key=lambda i: ts[i])
    if not ids:
        print("evaluate: no labels for this run's photos")
        return {}
    out = {"all": metrics(ids, labels, dec, ts, name)}
    for a in sorted({album[i] for i in ids}):
        sub = [i for i in ids if album[i] == a]
        if len(sub) >= 5:
            out[PurePosixPath(a).name] = metrics(sub, labels, dec, ts, name)
    head = f"{'':34s} {'photos':>6s} {'short P/R':>11s} {'long P/R':>11s} {'keep u/r':>9s} {'false arch':>10s} {'false keep':>10s} {'top1':>6s}"
    print(head)
    for k, m in out.items():
        s, lg = m["pairs_short"], m["pairs_long"]
        f = lambda x: "-" if x is None else f"{x:.2f}"
        print(f"{k[:34]:34s} {m['photos']:6d} {f(s['precision']):>5s}/{f(s['recall']):<5s} {f(lg['precision']):>5s}/{f(lg['recall']):<5s}"
              f" {m['user_keep']:4d}/{m['run_keep']:<4d} {f(m['false_archive_rate']):>10s} {f(m['false_keep_rate']):>10s} {m['top1']:>6s}")
    if verbose:
        print(json.dumps({k: {"false_archives": m["false_archives"], "false_keeps": m["false_keeps"]} for k, m in out.items()},
                         indent=1, ensure_ascii=False))
    return out
