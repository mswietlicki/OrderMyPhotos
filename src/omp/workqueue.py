"""A stage's photos as one queue that several workers take from, each as fast as it can (user, 2026-11-05: "make a
queue and subscriber workers running on different components but doing the same job as fast as they can; in the
future we could even use different machines in the local network"). P60.

- **The queue** holds the items. A worker takes the next chunk when it has a free slot, so a fast worker takes
  more, and the slow ones don't hold anyone up.
- **The CPU** is the stage's own process pool, with `cpu_slots` processes. The stage's thread tops it up from the
  queue (`take(1, cpu=True)`).
- **HTTP workers** run `omp worker`'s engine for the stage:
  - the 4090: llama-swap's omp-vision-cuda, only while the 4090 may be borrowed (helper.Gate);
  - the B50: a second `omp worker` the stage starts in the app's container, on 127.0.0.1 (Local);
  - another machine: just a URL. It must read the originals at the same paths, and only returns results.
- **An item a GPU worker can't do** (`{"error"}`) goes to the CPU (`cpu_only`): the CPU path is the reference
  and marks what it must (a truncated JPEG, say).
- **A worker that keeps failing** gives its chunks back. It stops after `give_up` seconds for a short step (the
  previews); for the long vision stages it waits for as long as it takes (the 4090 back from someone's chat).
- **Another machine** doesn't share Mimir's disks: it gets the files themselves (`send="bytes"`, base64), which is
  cheap for previews (~300 KB a photo).
- Results come out of `out`, for the stage's thread to write. `busy` counts the items workers have in hand, so a
  consumer knows whether more results will come.
"""

import base64
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque

GIVE_UP = 600  # seconds a worker may be unavailable, or failing, before it stops for this pass


class WorkQueue:
    def __init__(self, items):
        self.items, self.cpu_only = deque(items), deque()
        self.lock, self.out, self.stop = threading.Lock(), queue.Queue(), False
        self.done_by: dict[str, int] = {}
        self.busy = 0  # items a worker has taken and not answered yet

    def take(self, n: int, cpu: bool = False) -> list:
        """The next `n` items; `cpu`: for the stage's own device, which gets what the workers couldn't do first."""
        with self.lock:
            chunk = []
            while cpu and self.cpu_only and len(chunk) < n:
                chunk.append(self.cpu_only.popleft())
            while self.items and len(chunk) < n:
                chunk.append(self.items.popleft())
            if not cpu:
                self.busy += len(chunk)
            return chunk

    def give_back(self, chunk: list):
        with self.lock:
            self.items.extendleft(reversed(chunk))
            self.busy -= len(chunk)

    def to_cpu(self, item, taken: bool = False):
        """An item for the stage's own device; `taken`: one a worker had in hand."""
        with self.lock:
            self.cpu_only.append(item)
            self.busy -= taken

    def answer(self, name: str, item, result):
        """A worker's result for an item it had in hand."""
        with self.lock:
            self.busy -= 1
        self.out.put((name, item, result))

    def note(self, name: str, n: int = 1):
        with self.lock:
            self.done_by[name] = self.done_by.get(name, 0) + n


class HttpWorker:
    """`slots` threads, each sending chunks of `chunk` items to `url` ({"paths": [...]} -> {"results": [...]}) while
    `allowed()` says so. `path_of(item)` is what's sent; each result goes out as (name, item, result)."""

    def __init__(self, q: WorkQueue, name: str, url: str, path_of, chunk: int = 24, slots: int = 3, allowed=None,
                 backoff=None, send: str = "paths", give_up: float | None = GIVE_UP):
        self.q, self.name, self.url, self.path_of, self.chunk = q, name, url, path_of, chunk
        self.allowed, self.backoff = allowed or (lambda: True), backoff or (lambda: None)
        self.send, self.give_up = send, give_up
        self.unavailable, self.retired = 0.0, False
        self.threads = [threading.Thread(target=self._run, daemon=True, name=f"{name}-{k}") for k in range(slots)]
        for t in self.threads:
            t.start()

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.threads)

    def _run(self):
        import httpx

        client = httpx.Client(timeout=900)
        while not self.q.stop and not self.retired:
            if not self.allowed():
                self._wait("not free")
                continue
            chunk = self.q.take(self.chunk)
            if not chunk:
                time.sleep(0.5)  # nothing left, or given back later by a failing worker
                continue
            try:
                if self.send == "bytes":  # another machine: the files themselves
                    body = {"images": [base64.b64encode(open(self.path_of(it), "rb").read()).decode() for it in chunk]}
                else:
                    body = {"paths": [self.path_of(it) for it in chunk]}
                r = client.post(self.url, json=body)
                r.raise_for_status()
                results = r.json()["results"]
                if len(results) != len(chunk):
                    raise ValueError(f"{len(results)} results for {len(chunk)} photos")
            except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
                self.q.give_back(chunk)
                print(f"workqueue: {self.name} {type(exc).__name__} {str(exc)[:120]}; trying again")
                self.backoff()
                self._wait(type(exc).__name__)
                continue
            self.unavailable = 0.0
            for it, x in zip(chunk, results):
                if "error" in x:
                    self.q.to_cpu(it, taken=True)
                else:
                    self.q.answer(self.name, it, x)

    def _wait(self, why: str):
        if self.give_up is not None and self.unavailable >= self.give_up and not self.retired:
            self.retired = True
            print(f"workqueue: {self.name} unavailable for {self.give_up:.0f} s ({why}); the others go on without it")
            return
        end = time.monotonic() + 15
        while time.monotonic() < end and not self.q.stop:
            time.sleep(1)
        self.unavailable += 15


class Local:
    """A second `omp worker` in this container (the B50: torch sees the XPU here), on 127.0.0.1:`port`, for as long
    as the stage runs. `url` is None when it doesn't come up on the expected device."""

    def __init__(self, port: int, device: str = "xpu", log=None, pidfile=None):
        import httpx

        self.url, self.proc, self.pidfile = None, None, pidfile
        if pidfile and pidfile.exists():  # left by a stage that was killed: stop it, it holds the port and the GPU
            try:
                os.kill(int(pidfile.read_text()), 15)
                time.sleep(3)
            except (ValueError, OSError):
                pass
        env = os.environ | {"PORT": str(port), "OMP_WORKER_HOST": "127.0.0.1"}
        self.proc = subprocess.Popen([sys.executable, "-m", "omp", "worker"], env=env,
                                     stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if pidfile:
            pidfile.write_text(str(self.proc.pid))
        base = f"http://127.0.0.1:{port}"
        for _ in range(90):
            if self.proc.poll() is not None:
                break
            try:
                h = httpx.get(f"{base}/health", timeout=2).json()
                if h.get("device") == device:
                    self.url = base
                else:
                    print(f"workqueue: the local worker is on {h.get('device')}, not {device}; not used")
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(1)
        if self.url is None:
            self.close()

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.pidfile:
            self.pidfile.unlink(missing_ok=True)


def remotes() -> list[tuple[str, str]]:
    """OMP_REMOTE_WORKERS="name=http://host:port,...": `omp worker`s on other machines. They get the files in the
    request (send="bytes"), so they need no access to Mimir's disks."""
    out = []
    for part in os.environ.get("OMP_REMOTE_WORKERS", "").split(","):
        if "=" in part:
            name, url = part.split("=", 1)
            out.append((name.strip(), url.strip().rstrip("/")))
    return out
