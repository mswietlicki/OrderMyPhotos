"""omp <stage> [options]. Every stage is idempotent: it skips work already recorded for the same model."""

import argparse
import time
from datetime import datetime

from .config import Config
from .db import connect


def main(argv=None):
    ap = argparse.ArgumentParser(prog="omp")
    ap.add_argument("stage", choices=["inventory", "previews", "signals", "faces", "depth", "group", "rank",
                                      "judge", "captions", "report", "labels", "evaluate", "stats", "all"])
    ap.add_argument("--only", help="signals: comma list of dino,siglip,iqa")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--run", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--out", help="report: output directory")
    ap.add_argument("--file", help="labels: JSON exported from the report")
    args = ap.parse_args(argv)

    cfg = Config()
    con = connect(cfg.db)
    started = time.perf_counter()
    stages = ["inventory", "previews", "signals", "faces", "depth", "group", "rank", "judge", "captions",
              "report"] if args.stage == "all" else [args.stage]
    for stage in stages:
        if stage == "inventory":
            from . import inventory
            inventory.run(cfg, con, args.run)
        elif stage == "previews":
            from . import previews
            previews.run(cfg, con, args.run, force=args.force)
        elif stage == "signals":
            from . import vision
            only = (args.only or "dino,siglip,iqa").split(",")
            for name in only:
                getattr(vision, f"run_{name}")(cfg, con, args.run)
        elif stage == "faces":
            from . import faces
            faces.run(cfg, con, args.run)
        elif stage == "depth":
            from . import depth
            depth.run(cfg, con, args.run)
        elif stage == "group":
            from . import group
            group.run(cfg, con, args.run)
        elif stage == "rank":
            from . import rank
            rank.run(cfg, con, args.run)
        elif stage == "judge":
            from . import judge
            judge.run(cfg, con, args.run)
        elif stage == "captions":
            from . import captions
            captions.run(cfg, con, args.run)
        elif stage == "report":
            from . import report
            report.run(cfg, con, args.run, args.out)
        elif stage == "labels":
            from . import report
            report.import_labels(cfg, con, args.file)
        elif stage == "evaluate":
            from . import evaluate
            evaluate.run(cfg, con, args.run)
        elif stage == "stats":
            for r in con.execute("SELECT stage, items, round(seconds, 1) s, device FROM timing ORDER BY rowid"):
                print(dict(r))
    print(f"done in {time.perf_counter() - started:.1f} s")
