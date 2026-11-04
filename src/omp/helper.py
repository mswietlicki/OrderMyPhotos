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
"""

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
COUNTERS = ("llamacpp:tokens_predicted_total", "llamacpp:prompt_tokens_total")


class Gate:
    """Whether the 4090 may be borrowed now."""

    def __init__(self, base: str):
        self.base, self.client = base, httpx.Client(timeout=10)
        self.tokens, self.since = None, time.time()

    def users(self) -> set[str]:
        running = self.client.get(f"{self.base}/running").json().get("running", [])
        return {m["model"] for m in running if "nvidia.com/gpu" in (m.get("cmd") or "")}

    def allowed(self) -> bool:
        try:
            users = self.users()
        except (httpx.HTTPError, ValueError):
            return False
        if users <= {WORKER}:  # nothing else on the 4090
            return True
        if users - {WORKER, CHAT}:  # something else has it (the OCR, the long-context model)
            return False
        try:  # the chat model has it: borrowed only once it's been quiet for IDLE
            text = self.client.get(f"{self.base}/upstream/{CHAT}/metrics").text
            tokens = sum(float(line.split()[-1]) for line in text.splitlines() if line.startswith(COUNTERS))
        except (httpx.HTTPError, ValueError, IndexError):
            return False
        if tokens != self.tokens:
            self.tokens, self.since = tokens, time.time()
        return time.time() - self.since >= IDLE

    def backoff(self):
        self.tokens, self.since = None, time.time()


class Share:
    """A stage's photos from both ends: iterate it for (id, None) to do here, or (id, result) done on the 4090."""

    def __init__(self, cfg, engine: str, ids: list[str]):
        self.cfg, self.ids = cfg, list(ids)
        self.lo, self.hi, self.back, self.busy = 0, len(self.ids), [], 0
        self.lock, self.results, self.closed = threading.Lock(), queue.Queue(), False
        self.url = f"{cfg.llm}/upstream/{WORKER}/v1/{engine}"
        self.remote, self.seconds = 0, 0.0
        if ENABLED and len(self.ids) > 2 * CHUNK:
            threading.Thread(target=self._helper, daemon=True).start()

    def note(self) -> str:
        return f" + 4090 ({self.remote} photos)" if self.remote else ""

    def __iter__(self):
        try:
            while True:
                while not self.results.empty():
                    yield self.results.get_nowait()
                with self.lock:
                    if self.back:  # a chunk the 4090 gave back
                        i = self.back.pop()
                    elif self.lo < self.hi:
                        i, self.lo = self.ids[self.lo], self.lo + 1
                    else:
                        i = None
                    busy = self.busy
                if i is not None:
                    yield i, None
                elif busy:  # the last chunk is still on the 4090
                    try:
                        yield self.results.get(timeout=1)
                    except queue.Empty:
                        pass
                else:
                    break
            while not self.results.empty():
                yield self.results.get_nowait()
        finally:
            self.closed = True

    def _helper(self):
        gate, client = Gate(self.cfg.llm), httpx.Client(timeout=900)
        while not self.closed:
            with self.lock:
                if self.hi - self.lo <= CHUNK:  # the rest is the B50's
                    return
            if not gate.allowed():
                self._sleep(30)
                continue
            with self.lock:
                n = min(CHUNK, self.hi - self.lo - CHUNK)
                if n <= 0:
                    return
                batch, self.hi, self.busy = self.ids[self.hi - n:self.hi], self.hi - n, self.busy + 1
            try:
                t0 = time.perf_counter()
                r = client.post(self.url, json={"paths": [str(self.cfg.preview(i)) for i in batch]})
                r.raise_for_status()
                out = r.json()["results"]
                for i, x in zip(batch, out, strict=True):
                    self.results.put((i, x))
                self.remote += len(batch)
                self.seconds += time.perf_counter() - t0
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                with self.lock:
                    self.back.extend(batch)  # the B50 does them
                print(f"4090 worker: {type(exc).__name__} {str(exc)[:120]}; the B50 goes on alone for now")
                gate.backoff()
                self._sleep(60)
            finally:
                with self.lock:
                    self.busy -= 1

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.closed:
            time.sleep(1)
