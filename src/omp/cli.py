"""omp <stage> [<stage> ...] [options]. Every stage is idempotent: it skips work already recorded for the same
model, so a stopped run is resumed by running the same command again (P43). `omp serve` is the web app."""

import argparse
import sys
import time
from datetime import datetime

from . import runtime
from .config import Config
from .db import connect

STAGES = ["inventory", "previews", "signals", "orient", "faces", "subjects", "depth", "text", "group", "rank",
          "judge", "captions", "people", "place", "albums", "report", "plan", "apply", "ocr", "labels", "evaluate", "stats"]
ALL = ["inventory", "previews", "signals", "orient", "faces", "subjects", "depth", "text", "people", "place", "group", "rank",
       "judge", "captions", "albums", "report", "plan"]


def lock(cfg: Config):
    """One writer per DB: a second `omp` on the same library exits instead of doing the same work twice."""
    try:
        import fcntl
    except ImportError:  # Windows: tests only
        return None
    f = open(cfg.db.with_name(cfg.db.name + ".lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"omp: another run holds {f.name}")
    return f


# Quick stages without one loop to follow: a step over all photos, done when the stage is, so the app shows
# every step in photos and percent. The others count their own photos as they go.
WHOLE = {"people", "place", "plan"}


def run_stage(stage, cfg, con, args):
    if stage in WHOLE:
        n = con.execute("SELECT count(*) FROM photo").fetchone()[0]
        with runtime.Progress(con, stage, "cpu", total=n, commit=False) as prog:
            _run_stage(stage, cfg, con, args)
            prog.done(stage, n)
    else:
        _run_stage(stage, cfg, con, args)


def _run_stage(stage, cfg, con, args):
    if stage == "inventory":
        from . import inventory
        inventory.run(cfg, con, args.run)
    elif stage == "previews":
        from . import previews
        previews.run(cfg, con, args.run, force=args.force)
    elif stage == "signals":
        from . import vision
        only = (args.only or "dino,siglip,iqa,nudity").split(",")
        for name in only:
            getattr(vision, f"run_{name}")(cfg, con, args.run)
    elif stage == "faces":
        from . import faces
        faces.run(cfg, con, args.run)
    elif stage == "subjects":
        from . import subjects
        subjects.run(cfg, con, args.run)
    elif stage == "depth":
        from . import depth
        depth.run(cfg, con, args.run)
    elif stage == "text":
        from . import text
        text.run(cfg, con, args.run)
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
    elif stage == "people":
        from . import people
        people.run(cfg, con, args.run)
    elif stage == "plan":
        from . import plan
        plan.run(cfg, con, args.run)
    elif stage == "apply":  # the plan, done on the session's own copy (a test set); never on OneDrive
        from . import apply
        apply.undo(cfg) if args.undo else apply.run(cfg, con, args.run)
    elif stage == "orient":
        from . import orient
        orient.run(cfg, con, args.run)
    elif stage == "ocr":  # a post-process, not part of `all`: once the albums are done and photos sorted
        from . import text
        text.run_ocr(cfg, con, args.run)
    elif stage in ("place", "albums"):
        from . import albums
        albums.run(cfg, con, args.run, place_only=stage == "place")
    elif stage == "report":
        from . import report
        report.run(cfg, con, args.run, args.out)
    elif stage == "labels":
        from . import report
        report.import_labels(cfg, con, args.file)
    elif stage == "evaluate":
        from . import evaluate
        evaluate.run(cfg, con, args.run, verbose=args.force)
    elif stage == "stats":
        for r in con.execute("SELECT stage, items, round(seconds, 1) s, device FROM timing ORDER BY rowid"):
            print(dict(r))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="omp")
    ap.add_argument("stage", nargs="+", choices=STAGES + ["all", "serve", "worker"])
    ap.add_argument("--only", help="signals: comma list of dino,siglip,iqa,nudity")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--run", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--out", help="report: output directory")
    ap.add_argument("--file", help="labels: JSON exported from the report")
    ap.add_argument("--undo", action="store_true", help="apply: move the last apply's photos back")
    ap.add_argument("--lanes", action="store_true", help="run the stages side by side, one lane per device (P51)")
    args = ap.parse_args(argv)

    if args.stage == ["serve"]:
        from . import web
        return web.main()
    if args.stage == ["worker"]:  # the 4090's share of the vision stages, started by llama-swap (P49)
        from . import worker
        return worker.main()
    stages = [s for a in args.stage for s in (ALL if a == "all" else [a])]
    cfg = Config()
    held = lock(cfg)  # noqa: F841 - held until the process exits
    runtime.install(lanes=args.lanes)
    con = connect(cfg.db)
    started = time.perf_counter()
    runtime.report(run=args.run, stages=stages, done=[], steps=[], stage=None, step=None, error=None,
                   started=time.time(), state="running")
    done = []
    try:
        if args.lanes:
            from . import lanes
            t0 = time.time()
            runtime.report(stage="lanes", stage_started=t0, step=None)
            lanes.run(cfg, stages, args)
            done.append({"stage": "lanes", "seconds": round(time.time() - t0, 1)})
            runtime.report(done=done)
            stages = []
        for stage in stages:
            t0 = time.time()
            runtime.report(stage=stage, stage_started=t0, step=None)
            run_stage(stage, cfg, con, args)
            done.append({"stage": stage, "seconds": round(time.time() - t0, 1)})
            runtime.report(done=done)
    except runtime.Stopped:
        con.commit()
        runtime.report(state="stopped", stage=None)
        print(f"stopped after {time.perf_counter() - started:.1f} s; the same command resumes where it left off")
        sys.exit(143)
    except BaseException as exc:
        runtime.report(state="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        raise
    runtime.report(state="finished", stage=None, step=None)
    print(f"done in {time.perf_counter() - started:.1f} s")
