"""`omp serve`: the OrderMyPhotos web app (P44, P47). A wizard that guides one session (one root directory)
through its phases: the server's work runs on its own, and it stops where the reviewer is needed (naming
people, reviewing the albums, the final decision), with the review pages in the app itself.

The pipeline runs as a child process (`python -m omp <stages> --run <session>`), so a pause is a clean stop
after the photo in hand (P43). The wanted state is kept on disk (`control.json` in the session's folder), so
after a crash, a redeploy or a reboot the run resumes on its own, and a failing run is retried a few times
with a growing pause before it's marked failed.

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

from . import review, sessions
from .config import Policy
from .sessions import PHASE, PHASES
from .sysmon import Sampler

RETRY_AFTER = [30, 120, 600, 1800]   # seconds before restarting a run that failed, by attempt
MAX_RETRIES = 5
STOP_WAIT = 75                       # seconds a stopping run gets before it's killed (compose: 90 s)
HEALTHY = 600                        # seconds of running after which the retry count starts again
BUNDLE_URL = "OrderMyPhotos"         # the bundle's name at a library root, which its links assume
FRESH = {"phase": 0, "wanted": "idle", "retries": 0, "next_try": 0, "last_exit": None, "rounds": 0, "history": []}


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
    """Moves the active session through its phases. `wanted` is what the reviewer asked for: running,
    paused, review (waiting for the reviewer), finished or failed; the child process follows it."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.session: sessions.Session | None = None
        self.control: dict = dict(FRESH)
        self.bind(sessions.active())

    def bind(self, session: sessions.Session | None):
        with self.lock:
            if self.alive():
                raise HTTPException(409, "pause the running session first")
            self.session = session
            self.control = FRESH | _read_json(session.dir / "control.json", {}) if session else dict(FRESH)
            self.policy = Policy(session.config().policy_file) if session else None

    def save(self):
        if self.session:
            _write_json(self.session.dir / "control.json", self.control)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def phase(self) -> dict:
        return PHASES[min(self.control["phase"], len(PHASES) - 1)]

    def log_path(self) -> Path | None:
        return self.session.dir / "log.txt" if self.session else None

    def start(self):
        with self.lock:
            if not self.session:
                raise HTTPException(400, "make a session first")
            if self.alive():
                raise HTTPException(409, "already running")
            if self.phase["kind"] == "review":
                raise HTTPException(409, f"waiting for you: {self.phase['title']}")
            if self.control["wanted"] == "finished":
                raise HTTPException(409, "this session is finished")
            self.control.update(wanted="running", retries=0, next_try=0)
            self.save()
            self._spawn()

    def pause(self):
        with self.lock:
            if self.control["wanted"] in ("running", "failed"):
                self.control["wanted"] = "paused"
                self.save()
            if self.alive():
                self.proc.send_signal(signal.SIGTERM)  # the stage stops after the photo in hand

    def goto(self, key: str):
        """Run again from a phase (tests, or after a fix): everything after it is redone as needed."""
        with self.lock:
            if self.alive():
                raise HTTPException(409, "pause it first")
            self.control.update(phase=PHASE[key], wanted="paused", retries=0, next_try=0)
            if PHASES[PHASE[key]]["kind"] == "review":
                self.control["wanted"] = "review"
            self.save()

    def done(self, key: str, data: dict | None = None):
        """The reviewer finished a review phase: go on, or back to sorting what they took out."""
        with self.lock:
            if self.phase["key"] != key or self.phase["kind"] != "review":
                raise HTTPException(409, f"the current phase is {self.phase['title']}")
            if key == "review":
                pending = review.pending(self.session)
                if pending["unconfirmed"]:
                    raise HTTPException(409, f"{pending['unconfirmed']} albums aren't confirmed yet")
                if pending["to_sort"] and pending["to_sort"] == self.control.get("last_to_sort"):
                    raise HTTPException(409, f"{pending['to_sort']} photos found no other place in the last sort: "
                                             "move them by hand (M), or put them back where they were (O)")
                nxt = "resort" if pending["to_sort"] else "plan"
            elif key == "final":
                _write_json(self.session.dir / "final.json", (data or {}) | {"at": time.time()})
                self.control.update(phase=len(PHASES), wanted="finished")
                self.save()
                return
            else:
                nxt = PHASES[self.control["phase"] + 1]["key"]
            self.control.update(phase=PHASE[nxt], wanted="running", retries=0, next_try=0)
            self.save()
            if not self.alive():
                self._spawn()

    def _spawn(self):
        stages = self.phase["stages"]
        log = open(self.log_path(), "ab")
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S %Z')} {self.phase['title']}: {' '.join(stages)}\n".encode())
        log.flush()
        self.proc = subprocess.Popen([sys.executable, "-m", "omp", *stages, "--run", self.session.run_id],
                                     stdout=log, stderr=subprocess.STDOUT, env=os.environ | self.session.env(),
                                     cwd="/tmp" if os.name != "nt" else None)
        log.close()
        self.control.update(started=time.time(), pid=self.proc.pid)
        self.save()

    def _ended(self, code: int):
        c = self.control
        c["history"] = (c["history"] + [{"phase": self.phase["key"], "started": c.get("started"), "ended": time.time(),
                                          "exit": code}])[-50:]
        c.update(last_exit=code, pid=None)
        if code == 0:
            key = self.phase["key"]
            if key == "resort":  # back to the review, for what was just sorted
                c.update(phase=PHASE["review"], rounds=c["rounds"] + 1,
                         last_to_sort=review.pending(self.session)["to_sort"])
            else:
                c["phase"] += 1
            if c["phase"] >= len(PHASES):
                c["wanted"] = "finished"
            elif self.phase["kind"] == "review":
                c["wanted"] = "review"
            elif c["wanted"] == "running":
                self.save()
                self._spawn()  # the next server phase, now: no "stopped" moment in between
        elif c["wanted"] == "running":  # it crashed, or was stopped from outside: try again later
            n = c["retries"] = c["retries"] + 1
            if n > MAX_RETRIES:
                c["wanted"] = "failed"
            else:
                c["next_try"] = time.time() + RETRY_AFTER[min(n - 1, len(RETRY_AFTER) - 1)]
        self.save()

    def supervise(self):
        while True:
            with self.lock:
                if self.proc is not None and self.proc.poll() is not None:
                    code, self.proc = self.proc.returncode, None
                    self._ended(code)
                c = self.control
                if (self.session and c["wanted"] == "running" and self.phase["kind"] == "server" and not self.alive()
                        and time.time() >= c["next_try"]):
                    self._spawn()
                if self.alive() and c["retries"] and time.time() - c["started"] > HEALTHY:
                    c["retries"] = 0  # it got going again: the next failure starts the count afresh
                    self.save()
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
_counts: dict = {"at": 0, "db": None}


def library_counts(s: sessions.Session) -> dict:
    """What the session's DB holds, read-only and at most once a minute: it's a scan of the signal table."""
    db = s.config().db
    if time.time() - _counts["at"] < 60 and _counts["db"] == db:
        return _counts["data"]
    data = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        data["files"] = con.execute("SELECT count(*) FROM file").fetchone()[0]
        data["photos"] = con.execute("SELECT count(*) FROM photo").fetchone()[0]
        data["by_policy"] = dict(con.execute("SELECT policy, count(*) FROM file GROUP BY policy ORDER BY 2 DESC").fetchall())
        data["signals"] = dict(con.execute("SELECT kind, count(*) FROM signal GROUP BY kind ORDER BY kind").fetchall())
        data["errors"] = [dict(zip(("kind", "id", "data", "created"), r)) for r in con.execute(
            "SELECT kind, id, data, created FROM signal WHERE kind LIKE 'error.%' ORDER BY created DESC LIMIT 20")]
        data["timing"] = [dict(zip(("stage", "run", "items", "seconds", "device", "created"), r)) for r in con.execute(
            "SELECT stage, run, items, round(seconds, 1), device, created FROM timing ORDER BY rowid DESC LIMIT 25")]
        data["db_bytes"] = db.stat().st_size
        con.close()
    except (sqlite3.Error, OSError) as exc:
        data["error"] = "no data yet" if "unable to open" in str(exc) else str(exc)
    _counts.update(at=time.time(), db=db, data=data)
    return data


def gpu_models() -> list:
    """Which models llama-swap has loaded: a stage waits while another one holds the GPU it needs."""
    import httpx

    try:
        running = httpx.get(f"{os.environ.get('OMP_LLM', 'http://127.0.0.1:9292')}/running", timeout=2).json().get("running", [])
    except (httpx.HTTPError, ValueError):
        return []
    return [{k: m.get(k) for k in ("model", "state", "name")} for m in running]


def tail(path: Path | None, lines: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200 * lines))
            return b"\n".join(f.read().splitlines()[-lines:]).decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""


def state() -> str:
    c, alive = runner.control, runner.alive()
    if alive:
        return "running" if c["wanted"] == "running" else "stopping"
    if c["wanted"] == "running":  # between two phases, or waiting to try again after a failure
        return "retrying" if c["retries"] else "starting"
    return c["wanted"]


def status() -> dict:
    s, c, alive = runner.session, runner.control, runner.alive()
    out = {"session": None, "sessions": [{"id": x.id, "name": x.name, "root": x.root} for x in sessions.all_sessions()],
           "roots": [str(r) for r in sessions.ROOTS], "state": state(), "gpu": gpu_models(), "system": sampler.latest,
           "now": time.time()}
    if not s:
        return out
    st = _read_json(s.dir / "status.json", {})  # written by the running pipeline
    step = st.get("step") if alive else None
    if step:
        elapsed = max(time.time() - step["started"], 1e-6)
        step["rate"] = round(step["ok"] / elapsed, 2)
        left = (step["total"] or 0) - step["ok"] - step["failed"]
        step["eta"] = round(left / step["rate"]) if step["rate"] and step["total"] else None
    cur = c["phase"]
    phases = []
    for i, p in enumerate(PHASES):
        ran = [h for h in c["history"] if h["phase"] == p["key"] and h["exit"] == 0]
        phases.append({k: p[k] for k in ("key", "kind", "title", "about")} | {
            "state": "done" if i < cur else "current" if i == cur else "pending",
            "seconds": round(sum(h["ended"] - h["started"] for h in ran if h.get("started"))) if ran else None,
            "stages": p.get("stages")})
    if c["phase"] > PHASE["resort"] and not any(h["phase"] == "resort" for h in c["history"]):
        phases[PHASE["resort"]]["state"] = "skipped"
    pending = review.pending(s) if runner.phase["key"] in ("review", "resort", "plan", "final") else None
    out.update({
        "session": {"id": s.id, "name": s.name, "root": s.root, "library_index": s.library_index, "created": s.created},
        "phase": runner.phase["key"] if cur < len(PHASES) else None, "phases": phases, "rounds": c["rounds"],
        "wanted": c["wanted"], "stage": st.get("stage") if alive else None, "step": step,
        "steps": st.get("steps", []) if st.get("run") == s.run_id else [], "started": c.get("started"), "pid": c.get("pid"),
        "retries": c["retries"], "next_try": c["next_try"] if out["state"] == "retrying" else None,
        "last_exit": c["last_exit"], "error": st.get("error") if st.get("state") == "failed" else None,
        "history": c["history"][-12:], "review": pending, "library": library_counts(s),
        "final": _read_json(s.dir / "final.json", None)})
    return out


def _disks():
    s = runner.session
    return {"library": s.root, "state": str(s.dir), "cache": str(s.cache_dir)} if s else {}


sampler = Sampler(lambda: runner.proc.pid if runner.alive() else None, _disks)


@contextlib.asynccontextmanager
async def lifespan(app):
    threading.Thread(target=runner.supervise, daemon=True).start()
    sampler.start()
    yield
    runner.shutdown()


app = FastAPI(title="OrderMyPhotos", lifespan=lifespan)
app.include_router(review.router)
review.current = lambda: runner.session


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    return FileResponse(Path(__file__).parent / "favicon.svg", media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


def _page(name: str) -> HTMLResponse:
    """A page of the app, never kept by the browser: a deploy shows at the next load."""
    return HTMLResponse((Path(__file__).parent / name).read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse)
def page():
    return _page("web.html")


@app.get("/review", response_class=HTMLResponse)
def review_page():
    return _page("review.html")


@app.get("/people", response_class=HTMLResponse)
def people_page():
    return _page("people.html")


@app.get("/api/status")
def api_status():
    return status()


@app.post("/api/run/start")
def api_start():
    runner.start()
    return status()


@app.post("/api/run/pause")
def api_pause():
    runner.pause()
    return status()


@app.post("/api/phase/{key}/done")
def api_phase_done(key: str, data: dict | None = Body(None)):
    runner.done(key, data)
    return status()


@app.post("/api/phase/{key}/finish")
def api_phase_finish(key: str):
    """Finished: in the album review, everything not confirmed yet is accepted as it is; then the run goes on,
    as with Done."""
    if runner.phase["key"] != key or runner.phase["kind"] != "review":
        raise HTTPException(409, f"the current phase is {runner.phase['title']}")
    accepted = review.confirm_rest(runner.session) if key == "review" else 0
    runner.done(key)
    return status() | {"accepted": accepted}


@app.post("/api/phase/{key}/goto")
def api_phase_goto(key: str):
    if key not in PHASE:
        raise HTTPException(404)
    runner.goto(key)
    return status()


@app.get("/api/sessions/browse")
def api_browse(path: str | None = None):
    try:
        return sessions.browse(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/sessions")
def api_new_session(name: str = Body(...), root: str = Body(...), library_index: str | None = Body(None)):
    """A new session on a root directory; it becomes the active one."""
    if runner.alive():
        raise HTTPException(409, "pause the running session first")
    try:
        s = sessions.create(name, root, library_index)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    sessions.set_active(s.id)
    runner.bind(s)
    return status()


@app.post("/api/sessions/{id_}/activate")
def api_activate(id_: str):
    s = sessions.load(id_)
    if not s:
        raise HTTPException(404)
    runner.bind(s)
    sessions.set_active(s.id)
    return status()


@app.get("/api/history")
def api_history():
    """The last 10 minutes of resource samples, for the charts."""
    return list(sampler.history)


@app.get("/api/log", response_class=PlainTextResponse)
def api_log(lines: int = 200):
    return tail(runner.log_path(), min(lines, 5000))


@app.get("/library/{rel:path}")
def library_file(rel: str):
    """The session's photos and review bundle, read-only; folders left as they are stay hidden. The bundle is
    served as if it sat at the library root, wherever it's kept: its pages link to the originals by relative
    paths from there (`../../<path>`)."""
    s = runner.session
    if not s:
        raise HTTPException(404)
    cfg = s.config()
    base, sub = (cfg.bundle, rel[len(BUNDLE_URL) + 1:]) if rel.startswith(BUNDLE_URL + "/") else (cfg.root, rel)
    full = (base / sub).resolve()
    if not full.is_relative_to(base.resolve()) or not full.is_file() or (base == cfg.root and runner.policy(rel) == "leave"):
        raise HTTPException(404)
    return FileResponse(full)


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("OMP_WEB_PORT", "8790")), log_level="warning")
