"""Stages side by side, one lane per device (P51). In the phase that looks at every photo, the stages need
only the previews, except orient and text, which need SigLIP's embeddings and kinds. Run one after the
other, the B50, the CPU, the depth service and the 4090's chat model each wait for the others; in lanes
they work at the same time:

- B50: SigLIP 2, DINOv2, Q-ReAlign (with the 4090's help, P49), subjects;
- CPU: nudity, faces;
- depth: the depth service (on the B50, through llama-swap);
- VLM: orient and text, once SigLIP is done (orient before text, as one after the other).

Each lane is a thread with its own DB connection, in the one process the web app starts for the phase, so
the lock, the status file and the stop stay as they are (runtime.py). A lane that fails doesn't stop the
others; the phase fails at the end, and its retry redoes only what isn't recorded yet.
"""

import contextlib
import importlib
import os
import sys
import threading
import time
import traceback

from . import runtime
from .config import Config
from .db import connect

LANES = {
    "B50": ["siglip", "dino", "iqa", "subjects"],   # SigLIP first: the VLM lane waits for it
    "CPU": ["nudity", "faces"],
    "depth": ["depth"],
    "VLM": ["orient", "text"],
}
NEEDS = {"orient": "siglip", "text": "siglip"}
STEPS = {"dino": ("vision", "run_dino"), "siglip": ("vision", "run_siglip"), "iqa": ("vision", "run_iqa"),
         "nudity": ("vision", "run_nudity"), "orient": ("orient", "run"), "faces": ("faces", "run"),
         "subjects": ("subjects", "run"), "depth": ("depth", "run"), "text": ("text", "run")}
SIGNALS = "dino,siglip,iqa,nudity"
STOP_WAIT = 60   # seconds the lanes get to stop after a stop is asked (the web app kills at 75 s)


def plan(stages: list[str], only: str | None) -> dict[str, list[str]]:
    """The requested stages as lanes; `signals` is its four models (or --only's)."""
    steps = [x for s in stages for x in ((only or SIGNALS).split(",") if s == "signals" else [s])]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise SystemExit(f"omp --lanes: {', '.join(unknown)} can't run in a lane")
    return {name: [s for s in lane if s in steps] for name, lane in LANES.items() if any(s in steps for s in lane)}


def run(cfg: Config, stages: list[str], args):
    lanes = plan(stages, args.only)
    lane_of = {s: name for name, steps in lanes.items() for s in steps}
    done = {s: threading.Event() for s in lane_of}
    state = {name: "queued" for name in lanes}
    errors: dict[str, str] = {}

    def lane(name: str, steps: list[str]):
        runtime.set_lane(name)
        con = connect(cfg.db)
        runtime.report(state="running", steps=steps, finished=[], started=time.time())
        state[name] = "running"
        try:
            for step in steps:
                need = NEEDS.get(step)
                if need in done and not done[need].is_set():
                    runtime.report(state="waiting", current=step, waits_for=need, step=None)
                    while not done[need].wait(1):
                        runtime.check()
                        if state[lane_of[need]] in ("failed", "stopped"):
                            raise RuntimeError(f"{step} needs {need}, which didn't finish")
                runtime.report(state="running", current=step, waits_for=None, step_started=time.time())
                module, fn = STEPS[step]
                getattr(importlib.import_module(f".{module}", __package__), fn)(cfg, con, args.run)
                con.commit()
                done[step].set()
                runtime.report(finished=steps[:steps.index(step) + 1])
            state[name] = "done"
            runtime.report(state="done", current=None, step=None, ended=time.time())
        except runtime.Stopped:
            state[name] = "stopped"
            runtime.report(state="stopped", step=None, ended=time.time())
        except BaseException as exc:  # noqa: BLE001 - one lane's failure is reported; the others go on
            errors[name] = f"{type(exc).__name__}: {exc}"[:1000]
            state[name] = "failed"
            traceback.print_exc()
            runtime.report(state="failed", error=errors[name], step=None, ended=time.time())
        finally:
            with contextlib.suppress(Exception):
                con.commit()
                con.close()

    threads = [threading.Thread(target=lane, args=(n, s), name=f"lane-{n}", daemon=True) for n, s in lanes.items()]
    print("lanes: " + "; ".join(f"{n}: {' → '.join(s)}" for n, s in lanes.items()), flush=True)
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(1)
        if runtime.stopping():
            end = time.monotonic() + STOP_WAIT
            while any(t.is_alive() for t in threads) and time.monotonic() < end:
                time.sleep(1)
            if any(t.is_alive() for t in threads):  # one is loading a model and can't stop: nothing to commit
                print(f"lanes: {', '.join(t.name for t in threads if t.is_alive())} still busy; stopping anyway",
                      flush=True)
                runtime.report(state="stopped", stage=None)
                sys.stdout.flush()
                os._exit(143)
            raise runtime.Stopped("stop requested")
    if errors:
        raise RuntimeError("; ".join(f"lane {n}: {e}" for n, e in errors.items()))
