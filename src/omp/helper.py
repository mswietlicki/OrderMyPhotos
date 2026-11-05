"""The 4090 helping the B50 (P49, user: "if the 4090 is idle, and will be for some time, it could also do the
same job as the B50").

A stage that runs on the B50 works through its photos from the front. While the 4090 is free, a thread sends
chunks from the back to `omp worker` on the 4090 (llama-swap's `omp-vision-cuda`, which evicts the chat model
while it works), and the stage writes those results too. The two ends meet in the middle; a photo is never
done twice.

The 4090 is borrowed only while the chat model hasn't produced a token for OMP_4090_IDLE seconds (15 min).
Someone who chats gets it back: their request makes llama-swap swap the chat model in, the gate sees it
loaded and active again, and the helper waits for the next quiet stretch. Not the other way round: the 4090's
own work (the chat model, Q-ReAlign on CUDA kernels) never moves to the B50.

This process's own chat calls (a lane asking the chat model, P51) close the gate while they run and for a
minute after, so they and the worker don't swap the 4090 back and forth; and their tokens aren't someone
chatting, so they don't restart the quiet stretch. One gate per process, so it survives from stage to stage.
"""

import contextlib
import os
import queue
import threading
import time

import httpx

WORKER = "omp-vision-cuda"
CHAT = "qwen3.8-27b"
IDLE = float(os.environ.get("OMP_4090_IDLE", "900"))   # seconds without a chat token before the 4090 is borrowed
ENABLED = os.environ.get("OMP_4090", "1") != "0"
CHUNK = int(os.environ.get("OMP_4090_CHUNK", "24"))     # photos per request: a chat waits at most for one chunk
# onnxruntime threads for a stage's own CPU work while workers help (faces, nudity, P61): by default a CPU session takes
# every core, which the GPU workers need to prepare their images (the vision phase sat at 98% CPU, the 4090 at 7%)
LOCAL_ORT_THREADS = int(os.environ.get("OMP_LOCAL_ORT_THREADS", "4"))


def ort_options(threads: int | None):
    """onnxruntime SessionOptions with `threads` intra-op threads, or None for its default."""
    if not threads:
        return None
    import onnxruntime

    so = onnxruntime.SessionOptions()
    so.intra_op_num_threads, so.inter_op_num_threads = threads, 1
    return so
COUNTERS = ("llamacpp:tokens_predicted_total", "llamacpp:prompt_tokens_total")
OWN_GRACE = 60   # seconds after this process's own chat calls before the 4090 may be borrowed

_own = {"busy": 0, "last": 0.0}
_own_lock = threading.Lock()


@contextlib.contextmanager
def own_chat():
    """Around this process's own requests to the chat model (vlm.py)."""
    with _own_lock:
        _own["busy"] += 1
    try:
        yield
    finally:
        with _own_lock:
            _own["busy"] -= 1
            _own["last"] = time.time()


class Gate:
    """Whether the 4090 may be borrowed now."""

    def __init__(self, base: str):
        self.base, self.client = base, httpx.Client(timeout=10)
        self.tokens, self.since, self.looked = None, time.time(), 0.0
        self.lock = threading.Lock()

    def users(self) -> set[str]:
        running = self.client.get(f"{self.base}/running").json().get("running", [])
        return {m["model"] for m in running if "nvidia.com/gpu" in (m.get("cmd") or "")}

    def allowed(self) -> bool:
        with self.lock:
            try:
                users = self.users()
            except (httpx.HTTPError, ValueError):
                return False
            own = _own["busy"] > 0 or time.time() - _own["last"] < OWN_GRACE
            if users <= {WORKER}:  # nothing else on the 4090
                return not own
            if users - {WORKER, CHAT}:  # something else has it (the OCR, the long-context model)
                return False
            try:  # the chat model has it: borrowed only once it's been quiet for IDLE
                text = self.client.get(f"{self.base}/upstream/{CHAT}/metrics").text
                tokens = sum(float(line.split()[-1]) for line in text.splitlines() if line.startswith(COUNTERS))
            except (httpx.HTTPError, ValueError, IndexError):
                return False
            if tokens != self.tokens:
                if self.tokens is not None and not own and _own["last"] < self.looked:
                    self.since = time.time()  # someone chatted: not this process
                self.tokens = tokens
            self.looked = time.time()
            return not own and time.time() - self.since >= IDLE

    def backoff(self):
        with self.lock:
            self.tokens, self.since = None, time.time()


_gates: dict[str, Gate] = {}
_gates_lock = threading.Lock()


def gate(base: str) -> Gate:
    with _gates_lock:
        if base not in _gates:
            _gates[base] = Gate(base)
        return _gates[base]


class Share:
    """A stage's photos as a work queue (workqueue.py, P60). Iterate it for (id, None): a photo to do here, on the
    stage's own device (the B50), one at a time; or (id, result): one a worker did. The workers take chunks of
    CHUNK as fast as each can: the 4090 while it may be borrowed (the gate), and OMP_REMOTE_WORKERS, which get the
    previews' bytes. A photo a worker couldn't do comes back to be done here. Before P60 the B50 took from the
    front and the 4090 from the back of a list (P49)."""

    def __init__(self, cfg, engine: str, ids: list[str], extra=()):
        """`extra`: functions (queue, path_of) -> a worker of the stage's own (depth: the B50's depth service)."""
        from . import workqueue

        self.q = workqueue.WorkQueue(list(ids))
        path_of = lambda i: str(cfg.preview(i))  # noqa: E731
        self.workers = [make(self.q, path_of) for make in extra]
        if ENABLED and len(ids) > 2 * CHUNK:
            g = gate(cfg.llm)
            self.workers.append(workqueue.HttpWorker(self.q, "4090", f"{cfg.llm}/upstream/{WORKER}/v1/{engine}", path_of,
                                                     CHUNK, 1, allowed=g.allowed, backoff=g.backoff, give_up=None))
        for name, url in workqueue.remotes():
            self.workers.append(workqueue.HttpWorker(self.q, name, f"{url}/v1/{engine}", path_of, CHUNK, 1,
                                                     send="bytes", give_up=None))

    @property
    def remote(self) -> int:
        return sum(self.q.done_by.values())

    def note(self) -> str:
        return "".join(f" + {k} ({v} photos)" for k, v in self.q.done_by.items())

    def __iter__(self):
        try:
            while True:
                try:
                    name, i, x = self.q.out.get_nowait()
                    self.q.note(name)
                    yield i, x
                    continue
                except queue.Empty:
                    pass
                got = self.q.take(1, cpu=True)
                if got:
                    yield got[0], None
                elif self.q.busy:  # the last chunks are still with a worker
                    try:
                        name, i, x = self.q.out.get(timeout=1)
                        self.q.note(name)
                        yield i, x
                    except queue.Empty:
                        pass
                else:
                    break
        finally:
            self.q.stop = True
