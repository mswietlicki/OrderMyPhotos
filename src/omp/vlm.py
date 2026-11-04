"""Client for the local VLM (Qwen3.8-27B, thinking off) behind llama-swap. Images never leave Mimir:
they are read from the preview cache and sent to 127.0.0.1 only."""

import base64
import io
import json
import math
import time

import httpx
from PIL import Image

from .runtime import post

MODEL = "qwen3.8-27b:instruct"   # the name results are recorded under (judge, captions, text checks)
# What is asked for: the same loaded model, but without llama-swap's ":instruct" filter, which overrides
# every request's temperature (0.7), top_p (0.8) and presence_penalty (1.5), so "temperature 0" calls
# weren't (P46). Thinking stays off through the chat template. The yes/no logprobs are the same either way.
SERVED = {"qwen3.8-27b:instruct": "qwen3.8-27b"}
IMAGE_PX = 1152  # long side; ~1 MP at 4:3, about 1,000 image tokens for Qwen3.x


def image_part(path, long_side=IMAGE_PX) -> dict:
    """An image message part from a file path or a PIL image."""
    img = (path if isinstance(path, Image.Image) else Image.open(path)).convert("RGB")
    if max(img.size) > long_side:
        s = long_side / max(img.size)
        img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


class VLM:
    def __init__(self, base_url: str, model: str = MODEL, repeatable: bool = False):
        """`repeatable`: the same question gets the same answer, at the cost of recomputing the whole prompt
        each call. The server's reuse of a cached prompt prefix makes long answers drift (S11, P45)."""
        self.url = f"{base_url}/v1/chat/completions"
        self.model = model
        self.served = SERVED.get(model, model)
        self.extra = {"chat_template_kwargs": {"enable_thinking": False}} | ({"cache_prompt": False} if repeatable else {})
        self.client = httpx.Client(timeout=600)
        self.calls, self.seconds, self.prompt_tokens, self.output_tokens = 0, 0.0, 0, 0

    def _post(self, body):
        t0 = time.perf_counter()
        r = post(self.client, self.url, json={"model": self.served, **self.extra, **body})
        d = r.json()
        self.calls += 1
        self.seconds += time.perf_counter() - t0
        u = d.get("usage", {})
        self.prompt_tokens += u.get("prompt_tokens", 0)
        self.output_tokens += u.get("completion_tokens", 0)
        return d

    def choice(self, content: list, options: list[str], system: str | None = None) -> dict[str, float]:
        """Probability of each single-token answer, read from the first token's logprobs."""
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        d = self._post({"messages": messages, "max_tokens": 1, "temperature": 0, "logprobs": True, "top_logprobs": 20})
        top = d["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        lp = {}
        for t in top:
            tok = t["token"].strip().lower()
            if tok in options and tok not in lp:
                lp[tok] = t["logprob"]
        if not lp:
            return {}
        z = sum(math.exp(v) for v in lp.values())
        return {o: math.exp(lp[o]) / z if o in lp else 0.0 for o in options}

    def json(self, content: list, schema: dict, system: str | None = None, max_tokens=1024, temperature=0.2) -> dict:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
        for budget in (max_tokens, max_tokens * 3):  # an answer cut off at the budget is retried once with more
            d = self._post({
                "messages": messages, "max_tokens": budget, "temperature": temperature,
                "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema, "strict": True}},
            })
            try:
                return json.loads(d["choices"][0]["message"]["content"])
            except json.JSONDecodeError:
                if d["choices"][0].get("finish_reason") != "length":
                    raise
        raise ValueError(f"VLM answer still cut off at {max_tokens * 3} tokens")

    def stats(self) -> str:
        return (f"{self.calls} calls, {self.seconds:.0f} s ({self.seconds / max(self.calls, 1):.1f} s/call),"
                f" {self.prompt_tokens} prompt + {self.output_tokens} output tokens")
