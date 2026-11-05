"""S5 VLM judge for close calls, and the "worth keeping?" check for photos below the quality floor.

Every pair is asked in both orders and the answer is read from the logprobs of the `1`/`2` token, so
position bias cancels out and a parse failure can't turn into "A wins" (v1's failure mode). Three or
four contenders are compared round-robin and aggregated with Bradley–Terry.
"""

import itertools
import json
import time

from .config import Config
from .db import log_timing
from .runtime import Progress
from .vlm import VLM, image_part

TIE = 0.15           # |P(a better) - 0.5| below this is a tie: the feature ranking stands
FLOOR_KEEP = 0.5     # P("yes, keep") needed to overrule the quality floor
SUSPECT_KEEP = 0.2   # a photo only suspected (zero-shot "accidental") is archived only below this
MAX_CONTENDERS = 4

SYSTEM = ("You help cull a private photo library. You look at photos critically, like an experienced "
          "photographer choosing which shots to keep.")
PAIR = ("These two photos show the same scene, taken moments or minutes apart. Which one is better to keep? "
        "Judge the sharpness of the main subject, exposure, framing and composition, faces (eyes open, natural "
        "expression, nobody cut off or turned away) and whether it caught the moment. "
        "Answer with one digit: 1 for the first photo, 2 for the second.")
FLOOR = ("This photo may be blurred, badly exposed or taken by accident. Is it still worth keeping in a personal "
         "photo album? Keep it if it shows something recognizable that someone might want to see again, even if "
         "imperfect. Answer with one word: yes or no.")
# Reference photos (documents, labels, maps, boards...) are judged on legibility, not beauty.
FLOOR_REFERENCE = ("This photo may have been taken to keep information: a document, receipt, label, screen, map or "
                   "sign. Is it worth keeping? Keep it if it shows something recognizable, or if its information "
                   "(text, numbers, a map, a diagram) can still be read, even if the photo is ugly. Answer no only "
                   "if it is unusable: nothing recognizable and nothing readable. Answer with one word: yes or no.")


def bradley_terry(n, p, iters=200):
    """p[(i, j)] = P(i beats j). Returns strengths that sum to n."""
    w = [1.0] * n
    for _ in range(iters):
        new = []
        for i in range(n):
            num = sum(p[(i, j)] for j in range(n) if (i, j) in p)
            den = sum(1.0 / (w[i] + w[j]) for j in range(n) if (i, j) in p)
            new.append(max(num, 1e-6) / den if den else w[i])
        s = sum(new)
        w = [x * n / s for x in new]
    return w


def pair_prob(vlm, cfg, con, a, b):
    row = con.execute("SELECT p_a FROM judge WHERE a=? AND b=? AND model=?", (a, b, vlm.model)).fetchone()
    if row:
        return row[0]
    ia, ib = image_part(cfg.preview(a)), image_part(cfg.preview(b))
    ab = vlm.choice([ia, ib, {"type": "text", "text": PAIR}], ["1", "2"], SYSTEM)
    ba = vlm.choice([ib, ia, {"type": "text", "text": PAIR}], ["1", "2"], SYSTEM)
    if not ab or not ba:
        return None  # no verdict, never "a wins"
    p_ab, p_ba = ab["1"], ba["2"]
    p = (p_ab + p_ba) / 2
    for x, y, q in ((a, b, p), (b, a, 1 - p)):
        con.execute("INSERT OR REPLACE INTO judge(a, b, model, p_a, p_ab, p_ba, data) VALUES (?,?,?,?,?,?,?)",
                    (x, y, vlm.model, q, p_ab if x == a else p_ba, p_ba if x == a else p_ab, None))
    con.commit()
    return p


def run(cfg: Config, con, run_id: str):
    t0 = time.perf_counter()
    vlm = VLM(cfg.llm)
    dec = {r["id"]: dict(r) for r in con.execute("SELECT * FROM decision WHERE run=?", (run_id,))}
    for d in dec.values():
        d["features"] = json.loads(d["features"])
        d["reasons"] = json.loads(d["reasons"])

    floor_ids = [i for i, d in dec.items() if d["floor"] or d["features"].get("suspect")]
    bursts = {}
    for i, d in dec.items():
        if d["gid"] is not None:
            bursts.setdefault(d["gid"], []).append(i)
    close = [n for m in bursts.values() if (n := min(MAX_CONTENDERS, sum(1 for i in m if dec[i]["features"].get("judge")))) >= 2]
    # Progress in photos, for the app: each floor check, and the close frames of each burst compared
    with Progress(con, "judge", vlm.model, total=len(floor_ids) + sum(close)) as prog:
        # 1. Floor candidates: ask whether each is worth keeping.
        p_keep = {}
        for i in floor_ids:
            prog.done(i)
            reference = dec[i]["features"].get("purpose") in ("reference", "media")
            key = "floor:reference" if reference else "floor:memory"  # judge rows with b = the prompt used
            row = con.execute("SELECT p_a FROM judge WHERE a=? AND b=? AND model=?", (i, key, vlm.model)).fetchone()
            if row:
                p_keep[i] = row[0]
                continue
            prompt = FLOOR_REFERENCE if reference else FLOOR
            ans = vlm.choice([image_part(cfg.preview(i)), {"type": "text", "text": prompt}], ["yes", "no"], SYSTEM)
            if ans:
                p_keep[i] = ans["yes"]
                con.execute("INSERT OR REPLACE INTO judge(a, b, model, p_a) VALUES (?, ?, ?, ?)", (i, key, vlm.model, ans["yes"]))
                con.commit()

        # 2. Close calls inside bursts.
        judged = 0
        for gid, members in bursts.items():
            members.sort(key=lambda i: dec[i]["rank"])
            contenders = [i for i in members if dec[i]["features"].get("judge")][:MAX_CONTENDERS]
            if len(contenders) < 2:
                continue
            judged += 1
            prog.done(str(gid), len(contenders))
            idx = {c: k for k, c in enumerate(contenders)}
            p = {}
            for a, b in itertools.combinations(contenders, 2):
                q = pair_prob(vlm, cfg, con, a, b)
                if q is not None:
                    p[(idx[a], idx[b])], p[(idx[b], idx[a])] = q, 1 - q
            if not p:
                continue
            w = bradley_terry(len(contenders), p)
            feature_best = contenders[0]
            vlm_best = contenders[max(range(len(contenders)), key=lambda k: w[k])]
            for c in contenders:
                dec[c]["features"]["bt"] = round(w[idx[c]] / sum(w), 3)
            if vlm_best != feature_best:
                q = p.get((idx[vlm_best], idx[feature_best]))
                if q is not None and q - 0.5 >= TIE:
                    # The VLM overrules the features: the two frames swap verdicts (a burst archived as a whole
                    # stays archived; an extra frame kept for diversity stays kept).
                    vb, fb = dec[vlm_best], dec[feature_best]
                    vb["verdict"], fb["verdict"] = fb["verdict"], vb["verdict"]
                    vb["reasons"] = [f"best of burst: the VLM prefers it ({q:.0%} over the feature pick)"] + \
                        [r for r in vb["reasons"] if not r.startswith(("weaker", "best", "also kept"))]
                    fb["reasons"] = [f"the VLM prefers another frame ({q:.0%})"] + \
                        [r for r in fb["reasons"] if not r.startswith(("best", "also kept"))]
                    vb["tier"] = fb["tier"] = "review"
                else:
                    dec[feature_best]["reasons"].append(f"VLM: a tie with another frame ({q if q is not None else 0.5:.0%})")
            else:
                q = min(p[(idx[feature_best], idx[c])] for c in contenders if c != feature_best and (idx[feature_best], idx[c]) in p)
                dec[feature_best]["reasons"].append(f"the VLM agrees ({q:.0%} or more against each close frame)")
                if q - 0.5 >= TIE:
                    dec[feature_best]["tier"] = "auto" if not dec[feature_best]["floor"] else "review"

    # 3. Apply the floor verdicts.
    for gid, members in bursts.items():
        best = min(members, key=lambda i: dec[i]["rank"])
        if "whole burst below the quality floor" in " ".join(dec[best]["reasons"]) and p_keep.get(best, 0) >= FLOOR_KEEP:
            dec[best]["verdict"] = "keep"
            dec[best]["reasons"] = [r for r in dec[best]["reasons"] if r != "whole burst below the quality floor"] + \
                [f"VLM: worth keeping despite the floor ({p_keep[best]:.0%})"]
    for i in floor_ids:
        d = dec[i]
        if i not in p_keep:
            continue
        d["features"]["p_keep"] = round(p_keep[i], 3)
        if d["gid"] is None and not d["floor"]:  # only suspected: archive on a clear "no" only
            if p_keep[i] < SUSPECT_KEEP and not d["protected"]:
                d["verdict"] = "archive"
                d["reasons"] = ["bad on its own"] + d["reasons"][1:] + [f"VLM: not worth keeping ({1 - p_keep[i]:.0%})"]
            else:
                d["reasons"].append(f"VLM: worth keeping ({p_keep[i]:.0%})")
            d["tier"] = "review"
        elif d["gid"] is None:
            if p_keep[i] >= FLOOR_KEEP:
                d["verdict"] = "keep"
                d["reasons"].append(f"VLM: worth keeping ({p_keep[i]:.0%})")
            elif not d["protected"]:
                d["verdict"] = "archive"
                d["reasons"].append(f"VLM: not worth keeping ({1 - p_keep[i]:.0%})")
            d["tier"] = "review"

    for d in dec.values():
        con.execute("UPDATE decision SET verdict=?, tier=?, reasons=?, features=? WHERE id=? AND run=?",
                    (d["verdict"], d["tier"], json.dumps(d["reasons"]), json.dumps(d["features"]), d["id"], run_id))
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "judge", run_id, vlm.calls, dt, "4090 qwen3.8-27b")
    con.commit()
    keep = sum(1 for d in dec.values() if d["verdict"] == "keep")
    print(f"judge: {judged} bursts judged, {len(floor_ids)} floor checks, {vlm.stats()}; now keep {keep},"
          f" archive {len(dec) - keep}; {dt:.0f} s")
