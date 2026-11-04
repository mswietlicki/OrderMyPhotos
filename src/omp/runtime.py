"""How a run that takes days survives stops, crashes, reboots and model swaps (P43).

- A stage loop commits every few hundred photos or every 10 s, always between photos, so a stop or a
  crash loses at most that much. Run again, the stage skips what was committed.
- SIGTERM or SIGINT (`systemctl stop`, `docker stop`, Ctrl-C) ends a stage loop after the photo in hand.
  Outside a loop (model loading, the CPU stages that redo their whole run anyway) it stops at once.
- One photo that fails is logged as an `error.<stage>` signal and skipped. Many in a row stop the stage,
  because then it's the model or the GPU, not the photos.
- Calls to llama-swap wait out its restarts (~100 s) and model swaps instead of failing the stage.
"""

import contextlib
import json
import os
import signal
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import httpx

from .db import put_signal

# Short transactions: the web app writes labels into the same DB while a run goes on.
COMMIT_EVERY, COMMIT_SECONDS = 500, 10
MAX_FAILURES = 20        # photos failing in a row before the stage gives up
RETRY_FOR = 900          # seconds of llama-swap errors before a call gives up
REPORT_SECONDS = 3       # how often a stage loop rewrites the status file
STATUS = Path(os.environ.get("OMP_STATUS") or Path(os.environ.get("OMP_DB", "/state/omp.db")).with_name("status.json"))

_status: dict = {}


def report(**fields):
    """Update the run's status file, which `omp serve` reads for its progress page. Atomic, never fatal."""
    _status.update(fields, pid=os.getpid(), updated=time.time())
    try:
        tmp = STATUS.with_name(STATUS.name + ".tmp")
        tmp.write_text(json.dumps(_status), encoding="utf-8")
        os.replace(tmp, STATUS)
    except OSError:
        pass


class Stopped(Exception):
    """A stop was asked for. What was done is committed."""


_stop = False
_graceful = 0            # > 0 while a stage loop checks for the stop between photos


def _on_signal(signum, frame):
    global _stop
    _stop = True
    if not _graceful:
        raise Stopped(signal.Signals(signum).name)


def install():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)


def worker_init():
    """Pool workers leave the stop to the main process, which cancels what they haven't started."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


@contextlib.contextmanager
def worker_pool(workers: int, threads=False):
    """A process (or thread) pool that a stop can interrupt: the work not started yet is cancelled and only
    the items in hand finish, instead of the whole queue that `map` submitted up front."""
    pool = (ThreadPoolExecutor(max_workers=workers) if threads
            else ProcessPoolExecutor(max_workers=workers, initializer=worker_init))
    try:
        yield pool
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def check():
    if _stop:
        raise Stopped("stop requested")


def sleep(seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check()
        time.sleep(min(1.0, end - time.monotonic()))


class Progress:
    """`with Progress(con, stage, model) as prog:`, then `with prog.item(id_):` around each photo's work,
    or `prog.done(id_)` / `prog.fail(id_, error)` for results coming back from a process pool."""

    def __init__(self, con, stage: str, model: str, total: int | None = None, every=COMMIT_EVERY, seconds=COMMIT_SECONDS):
        self.con, self.stage, self.model, self.total = con, stage, model, total
        self.every, self.seconds = every, seconds
        self.ok = self.failed = self.streak = self.pending = 0
        self.last = self.reported = time.monotonic()
        self.started = time.time()
        self.samples = deque(maxlen=21)   # (time, ok) every REPORT_SECONDS: the speed over the last minute
        self.errors = deque(maxlen=5)     # the latest skipped photos, for the web app

    def __enter__(self):
        global _graceful
        _graceful += 1
        self._report()
        return self

    def __exit__(self, *exc):
        global _graceful
        _graceful -= 1
        self.con.commit()
        self._report()
        report(steps=(_status.get("steps", []) + [{
            "name": self.stage, "ok": self.ok, "failed": self.failed, "total": self.total,
            "seconds": round(time.time() - self.started, 1), "stopped": exc[0] is not None}])[-40:], step=None)
        if self.failed:
            print(f"{self.stage}: {self.failed} photos skipped (signal error.{self.stage})")
        return False

    @contextlib.contextmanager
    def item(self, id_: str):
        try:
            yield
        except (Stopped, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 - one bad photo skips that photo, not the stage
            self.fail(id_, f"{type(exc).__name__}: {exc}")
        else:
            self.done(id_)

    def done(self, id_: str):
        self.ok += 1
        self.streak = 0
        self.tick()

    def fail(self, id_: str, error: str):
        self.failed += 1
        self.streak += 1
        put_signal(self.con, id_, f"error.{self.stage}", self.model, data={"error": error[:500]})
        self.errors.append({"id": id_, "error": error[:300], "t": time.time()})
        print(f"{self.stage}: skipped {id_[:10]}: {error[:200]}")
        if self.streak >= MAX_FAILURES:
            self.con.commit()
            raise RuntimeError(f"{self.stage}: {self.streak} photos in a row failed, the last with {error[:200]}")
        self.tick()

    def tick(self):
        self.pending += 1
        now = time.monotonic()
        if _stop or self.pending >= self.every or now - self.last >= self.seconds:
            self.con.commit()
            self.pending, self.last = 0, now
        if now - self.reported >= REPORT_SECONDS:
            self._report()
        check()

    def _report(self):
        self.reported = time.monotonic()
        self.samples.append((time.time(), self.ok))
        t0, n0 = self.samples[0]
        recent = (self.ok - n0) / (time.time() - t0) if time.time() - t0 >= 5 else None
        report(step={"name": self.stage, "ok": self.ok, "failed": self.failed, "total": self.total,
                     "started": self.started, "recent_rate": round(recent, 2) if recent is not None else None,
                     "errors": list(self.errors)})


def post(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """POST to llama-swap. Connection errors, timeouts, 429 and 5xx are retried for up to RETRY_FOR seconds,
    which covers a llama-swap restart and a model swap. A 4xx is the request's own fault: it raises
    ValueError at once, so the caller can skip that photo."""
    delay, waited = 5, 0
    while True:
        try:
            r = client.post(url, **kw)
            if r.status_code < 500 and r.status_code != 429:
                break
            error = f"HTTP {r.status_code}"
        except httpx.TransportError as exc:
            error = type(exc).__name__
        if waited >= RETRY_FOR:
            raise RuntimeError(f"{url}: still failing after {waited} s ({error})")
        print(f"{url}: {error}, retrying in {delay} s")
        sleep(delay)
        waited += delay
        delay = min(delay * 2, 60)
    if r.status_code >= 400:
        raise ValueError(f"{url}: HTTP {r.status_code}: {r.text[:300]}")
    return r
