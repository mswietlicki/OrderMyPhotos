"""`omp serve`: the OrderMyPhotos web app (P44). A page and a JSON API to start and pause the pipeline,
follow its progress, read its log and open the results; the review pages are served from the library, and
their exports can be posted back instead of downloaded and imported by hand.

The pipeline runs as a child process (`python -m omp <stages> --run <run>`), so a pause is a clean stop
after the photo in hand (P43). The wanted state is kept on disk (`control.json` next to the DB), so after a
crash, a redeploy or a reboot the run resumes on its own, and a failing run is retried a few times with a
growing pause before it's marked failed.

It listens on 127.0.0.1 only; on Mimir, Tailscale Serve publishes it on the tailnet. API docs: /docs.
"""

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from . import cli, runtime
from .config import Config, Policy
from .sysmon import Sampler

# The library is processed in three passes, because each needs the one before and the user in between:
# the per-photo signals; then people, bursts and picks (the user names people on the People page); then
# captions, which use the names, and the albums, which read the captions.
PIPELINES = {
    "signals": {"title": "1 · Signals per photo",
                "stages": ["inventory", "previews", "signals", "orient", "faces", "subjects", "depth", "text"]},
    "people": {"title": "2 · People, bursts and picks",
               "stages": ["people", "place", "group", "rank", "judge", "report"]},
    "albums": {"title": "3 · Captions and albums", "stages": ["captions", "albums", "report", "plan"]},
    "all": {"title": "Everything", "stages": cli.ALL},
}
RETRY_AFTER = [30, 120, 600, 1800]   # seconds before restarting a run that failed, by attempt
MAX_RETRIES = 5
STOP_WAIT = 75                       # seconds a stopping run gets before it's killed (compose: 90 s)

cfg = Config()
STATE = cfg.db.parent
CONTROL = STATE / "control.json"
LOGS = STATE / "logs"
LABELS = STATE / "labels"
RUN_ID = os.environ.get("OMP_RUN", "full")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class Runner:
    """Keeps the pipeline child process matching the wanted state: running, paused, finished or failed."""

    def __init__(self):
        LOGS.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.control = {"wanted": "idle", "pipeline": "signals", "run": RUN_ID, "retries": 0, "next_try": 0,
                        "last_exit": None, "history": []} | _read_json(CONTROL, {})

    def save(self):
        _write_json(CONTROL, self.control)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def stages(self) -> list[str]:
        return PIPELINES[self.control["pipeline"]]["stages"]

    def log_path(self) -> Path:
        return LOGS / f"{self.control['run']}.log"

    def start(self, pipeline: str):
        with self.lock:
            if self.alive():
                raise HTTPException(409, "a run is going on; pause it first")
            self.control.update(wanted="running", pipeline=pipeline, retries=0, next_try=0)
            self.save()
            self._spawn()

    def pause(self):
        with self.lock:
            self.control["wanted"] = "paused"
            self.save()
            if self.alive():
                self.proc.send_signal(signal.SIGTERM)  # the stage stops after the photo in hand

    def _spawn(self):
        log = open(self.log_path(), "ab")
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S %Z')} start {' '.join(self.stages)}\n".encode())
        log.flush()
        self.proc = subprocess.Popen([sys.executable, "-m", "omp", *self.stages, "--run", self.control["run"]],
                                     stdout=log, stderr=subprocess.STDOUT, cwd="/tmp" if os.name != "nt" else None)
        log.close()
        self.control.update(started=time.time(), pid=self.proc.pid)
        self.save()

    def _ended(self, code: int):
        wanted = self.control["wanted"]
        entry = {"pipeline": self.control["pipeline"], "started": self.control.get("started"), "ended": time.time(),
                 "exit": code}
        self.control["history"] = (self.control["history"] + [entry])[-30:]
        self.control.update(last_exit=code, pid=None)
        if code == 0:
            self.control["wanted"] = "finished"
        elif wanted == "running":  # it crashed, or was stopped from outside: try again later
            n = self.control["retries"] = self.control["retries"] + 1
            if n > MAX_RETRIES:
                self.control["wanted"] = "failed"
            else:
                self.control["next_try"] = time.time() + RETRY_AFTER[min(n - 1, len(RETRY_AFTER) - 1)]
        self.save()

    def supervise(self):
        while True:
            with self.lock:
                if self.proc is not None and self.proc.poll() is not None:
                    code, self.proc = self.proc.returncode, None
                    self._ended(code)
                if self.control["wanted"] == "running" and not self.alive() and time.time() >= self.control["next_try"]:
                    self._spawn()
            time.sleep(2)

    def shutdown(self):
        """The app itself is stopping (redeploy, reboot): stop the run cleanly but keep it wanted, so the
        next start resumes it."""
        with self.lock:
            if not self.alive():
                return
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(STOP_WAIT)
            except subprocess.TimeoutExpired:
                self.proc.kill()


runner = Runner()
_counts: dict = {"at": 0}


def library_counts() -> dict:
    """What the DB holds, read-only and at most once a minute: it's a scan of the signal table."""
    if time.time() - _counts["at"] < 60:
        return _counts["data"]
    data = {}
    try:
        con = sqlite3.connect(f"file:{cfg.db}?mode=ro", uri=True, timeout=5)
        data["files"] = con.execute("SELECT count(*) FROM file").fetchone()[0]
        data["photos"] = con.execute("SELECT count(*) FROM photo").fetchone()[0]
        data["by_policy"] = dict(con.execute("SELECT policy, count(*) FROM file GROUP BY policy ORDER BY 2 DESC").fetchall())
        data["signals"] = dict(con.execute("SELECT kind, count(*) FROM signal GROUP BY kind ORDER BY kind").fetchall())
        data["errors"] = [dict(zip(("kind", "id", "data", "created"), r)) for r in con.execute(
            "SELECT kind, id, data, created FROM signal WHERE kind LIKE 'error.%' ORDER BY created DESC LIMIT 20")]
        data["timing"] = [dict(zip(("stage", "run", "items", "seconds", "device", "created"), r)) for r in con.execute(
            "SELECT stage, run, items, round(seconds, 1), device, created FROM timing ORDER BY rowid DESC LIMIT 25")]
        data["db_bytes"] = cfg.db.stat().st_size
        con.close()
    except (sqlite3.Error, OSError) as exc:
        data["error"] = str(exc)
    _counts.update(at=time.time(), data=data)
    return data


def gpu_models() -> list:
    """Which models llama-swap has loaded: a stage waits while another one holds the GPU it needs."""
    import httpx

    try:
        running = httpx.get(f"{cfg.llm}/running", timeout=2).json().get("running", [])
    except (httpx.HTTPError, ValueError):
        return []
    return [{k: m.get(k) for k in ("model", "state", "name")} for m in running]


def tail(path: Path, lines: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200 * lines))
            return b"\n".join(f.read().splitlines()[-lines:]).decode("utf-8", "replace")
    except OSError:
        return ""


def status() -> dict:
    c = runner.control
    alive = runner.alive()
    st = _read_json(runtime.STATUS, {})  # written by the running pipeline
    if alive:
        state = "running" if c["wanted"] == "running" else "stopping"
    elif c["wanted"] == "running":
        state = "retrying"
    else:
        state = c["wanted"]
    step = st.get("step") if alive else None
    if step:
        elapsed = max(time.time() - step["started"], 1e-6)
        step["rate"] = round(step["ok"] / elapsed, 2)
        left = (step["total"] or 0) - step["ok"] - step["failed"]
        step["eta"] = round(left / step["rate"]) if step["rate"] and step["total"] else None
    done = {d["stage"]: d["seconds"] for d in st.get("done", [])} if st.get("run") == c["run"] else {}
    stages = [{"stage": s, "state": "done" if s in done and not (alive and st.get("stage") == s) else
               "running" if alive and st.get("stage") == s else "pending", "seconds": done.get(s)}
              for s in runner.stages]
    results = sorted(p.name for p in cfg.bundle.glob("*.html")) if cfg.bundle.exists() else []
    steps = st.get("steps", []) if st.get("run") == c["run"] else []
    return {
        "state": state, "wanted": c["wanted"], "pipeline": c["pipeline"], "run": c["run"],
        "pipelines": {k: v for k, v in PIPELINES.items()}, "stages": stages, "stage": st.get("stage") if alive else None,
        "step": step, "started": c.get("started"), "pid": c.get("pid"), "retries": c["retries"],
        "next_try": c["next_try"] if state == "retrying" else None, "last_exit": c["last_exit"],
        "error": st.get("error") if st.get("state") == "failed" else None, "history": c["history"][-10:],
        "steps": steps, "results": [f"/library/{cfg.bundle.name}/{n}" for n in results], "library": library_counts(),
        "gpu": gpu_models(), "system": sampler.latest, "now": time.time(),
    }


sampler = Sampler(lambda: runner.proc.pid if runner.alive() else None,
                  {"library": str(cfg.root), "state": str(STATE), "cache": str(cfg.cache)})


@contextlib.asynccontextmanager
async def lifespan(app):
    threading.Thread(target=runner.supervise, daemon=True).start()
    sampler.start()
    yield
    runner.shutdown()


app = FastAPI(title="OrderMyPhotos", lifespan=lifespan)
policy = Policy(cfg.policy_file)


@app.get("/", response_class=HTMLResponse)
def page():
    return (Path(__file__).parent / "web.html").read_text(encoding="utf-8")


@app.get("/api/status")
def api_status():
    return status()


@app.get("/api/history")
def api_history():
    """The last 10 minutes of resource samples, for the charts."""
    return list(sampler.history)


@app.post("/api/run/start")
def api_start(pipeline: str = Body("signals", embed=True)):
    if pipeline not in PIPELINES:
        raise HTTPException(400, f"pipeline must be one of {', '.join(PIPELINES)}")
    runner.start(pipeline)
    return status()


@app.post("/api/run/pause")
def api_pause():
    runner.pause()
    return status()


@app.get("/api/log", response_class=PlainTextResponse)
def api_log(lines: int = 200):
    return tail(runner.log_path(), min(lines, 5000))


@app.post("/api/labels")
def api_labels(spec: dict = Body(...)):
    """A review page's export (album decisions, or the People page's names), imported at once."""
    from .db import connect
    from .report import import_labels

    LABELS.mkdir(parents=True, exist_ok=True)
    kind = "names" if spec.get("names") else "decisions"
    path = LABELS / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    con = connect(cfg.db)
    try:
        import_labels(cfg, con, str(path))
    finally:
        con.close()
    return {"saved": path.name}


@app.get("/library/{rel:path}")
def library_file(rel: str):
    """The review bundle and the originals it links to, read-only; folders left as they are stay hidden."""
    full = (cfg.root / rel).resolve()
    if not full.is_relative_to(cfg.root.resolve()) or not full.is_file() or policy(rel) == "leave":
        raise HTTPException(404)
    return FileResponse(full)


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("OMP_WEB_PORT", "8790")), log_level="warning")
