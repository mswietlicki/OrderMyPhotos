"""What a run costs the machine, sampled every few seconds for the web app: CPU, memory and disk I/O of the
pipeline's process tree and of the whole host, free space, and both GPUs:
- the B50's load from the xe driver's idle residency in sysfs, and the pipeline's own VRAM and engine
  cycles from its DRM clients' fdinfo;
- the 4090, and the B50's total VRAM, from the host's GPU stats file (mimir `mimir-gpu-stats`), since this
  container can't have the NVIDIA device: it breaks Level Zero for the B50.
Linux only; on other systems the fields are just missing.
"""

import json
import os
import re
import shutil
import threading
import time
from collections import deque

INTERVAL = 3
GPU_STATS = os.environ.get("OMP_GPU_STATS", "/run/mimir-gpu/stats.json")
B50_PCI = os.environ.get("OMP_B50_PCI", "0000:0e:00.0")
HISTORY = 200            # samples kept for the charts: 10 minutes
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def tree(root: int) -> list[int]:
    """root and all its descendants (pool workers, exiftool), from /proc."""
    parent = {}
    for d in os.listdir("/proc"):
        if d.isdigit() and (st := _read(f"/proc/{d}/stat")):
            parent[int(d)] = int(st.rsplit(")", 1)[1].split()[1])
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += [c for c, pp in parent.items() if pp == p]
    return out


def _host_cpu():
    f = (_read("/proc/stat") or "cpu 0").splitlines()[0].split()[1:]
    v = [int(x) for x in f]
    return sum(v), v[3] + (v[4] if len(v) > 4 else 0)  # total, idle + iowait


def _proc(pids):
    cpu = rss = rd = wr = 0
    for p in pids:
        if st := _read(f"/proc/{p}/stat"):
            f = st.rsplit(")", 1)[1].split()
            cpu += int(f[11]) + int(f[12])  # utime + stime, in ticks
        if s := _read(f"/proc/{p}/status"):
            m = re.search(r"VmRSS:\s+(\d+) kB", s)
            rss += int(m.group(1)) * 1024 if m else 0
        if io := _read(f"/proc/{p}/io"):
            kv = dict(line.split(": ") for line in io.splitlines() if ": " in line)
            rd += int(kv.get("read_bytes", 0))
            wr += int(kv.get("write_bytes", 0))
    return cpu, rss, rd, wr


def _b50(pids):
    """VRAM held on the B50 and engine cycle counters, summed over the DRM clients these processes hold."""
    vram, cycles, total, seen = 0, {}, {}, set()
    for p in pids:
        try:
            fds = os.listdir(f"/proc/{p}/fdinfo")
        except OSError:
            continue
        for fd in fds:
            text = _read(f"/proc/{p}/fdinfo/{fd}")
            if not text or "drm-driver" not in text:
                continue
            kv = {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in text.splitlines() if ":" in line)}
            if kv.get("drm-driver") != "xe" or kv.get("drm-client-id") in seen:
                continue
            seen.add(kv.get("drm-client-id"))
            if m := re.match(r"(\d+)\s*(\w+)?", kv.get("drm-resident-vram0") or kv.get("drm-total-vram0") or ""):
                vram += int(m.group(1)) * _UNITS.get(m.group(2) or "B", 1)
            for k, v in kv.items():
                if k.startswith("drm-cycles-"):
                    cycles[k[11:]] = cycles.get(k[11:], 0) + int(v.split()[0])
                elif k.startswith("drm-total-cycles-"):
                    total[k[17:]] = max(total.get(k[17:], 0), int(v.split()[0]))
    return vram, cycles, total


def _host_gpus():
    """The host's GPU stats file, if it's fresh: the 4090 and the B50's total VRAM."""
    try:
        d = json.loads(_read(GPU_STATS) or "")
    except ValueError:
        return None, None
    if time.time() - d.get("t", 0) > 30:
        return None, None
    n = d.get("nvidia") or {}
    nvidia = {"name": n.get("name"), "util": n.get("util"), "power": n.get("power_w"), "temp": n.get("temp_c"),
              "vram_used": n["mem_used_mib"] * 2 ** 20 if n.get("mem_used_mib") is not None else None,
              "vram_total": n["mem_total_mib"] * 2 ** 20 if n.get("mem_total_mib") is not None else None}
    return (nvidia if n.get("util") is not None else None), (d.get("b50") or {}).get("vram_used")


def _b50_idle() -> dict[str, int]:
    """Milliseconds each B50 GT (gt0 render and compute, gt1 media) has spent idle since boot."""
    out = {}
    for gt in ("gt0", "gt1"):
        if (v := _read(f"/sys/bus/pci/devices/{B50_PCI}/tile0/{gt}/gtidle/idle_residency_ms")) is not None:
            out[gt] = int(v)
    return out


def _memory():
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) * 1024
    return mem


class Sampler:
    """Background sampling. `pid()` returns the pipeline's process, or None while nothing runs."""

    def __init__(self, pid, disks: dict[str, str]):
        self.pid, self.disks = pid, disks
        self.history = deque(maxlen=HISTORY)
        self.latest: dict = {}
        self._prev = None

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001 - monitoring must never take the app down
                self.latest = {"error": str(exc)}
            time.sleep(INTERVAL)

    def sample(self):
        now = time.time()
        pid = self.pid()
        pids = tree(pid) if pid and os.path.exists("/proc") else []
        host_total, host_idle = _host_cpu() if os.path.exists("/proc/stat") else (0, 0)
        cpu, rss, rd, wr = _proc(pids)
        vram, cycles, total = _b50(pids)
        idle = _b50_idle()
        cur = {"t": now, "host": (host_total, host_idle), "cpu": cpu, "rd": rd, "wr": wr, "cycles": cycles, "total": total,
               "idle": idle}
        s = {"t": now, "processes": len(pids), "rss": rss, "b50_vram": vram, "cores": os.cpu_count()}
        if (p := self._prev) and now > p["t"]:
            dt = now - p["t"]
            dh = host_total - p["host"][0]
            s["host_cpu"] = round(100 * (1 - (host_idle - p["host"][1]) / dh), 1) if dh else None
            s["cpu"] = round(100 * (cpu - p["cpu"]) / _TICK / dt, 1)  # 100 = one core
            s["read"] = max(0, rd - p["rd"]) / dt
            s["write"] = max(0, wr - p["wr"]) / dt
            s["b50_busy"] = {e: round(100 * (cycles[e] - p["cycles"].get(e, 0)) / (total[e] - p["total"][e]), 1)
                             for e in cycles if e in total and e in p["total"] and total[e] > p["total"][e]}
            s["b50_load"] = {gt: round(max(0.0, min(100.0, 100 * (1 - (idle[gt] - p["idle"][gt]) / (1000 * dt)))), 1)
                             for gt in idle if gt in p["idle"]}
        self._prev = cur
        mem = _memory()
        s["mem_total"], s["mem_available"] = mem.get("MemTotal"), mem.get("MemAvailable")
        s["load"] = os.getloadavg() if hasattr(os, "getloadavg") else None
        s["gpu4090"], s["b50_vram_total_used"] = _host_gpus()
        s["disks"] = {}
        for name, path in self.disks.items():
            try:
                u = shutil.disk_usage(path)
                s["disks"][name] = {"free": u.free, "total": u.total}
            except OSError:
                pass
        self.latest = s
        self.history.append({k: s.get(k) for k in ("t", "host_cpu", "cpu", "rss", "read", "write")}
                            | {"gpu": (s["gpu4090"] or {}).get("util"), "b50": (s.get("b50_load") or {}).get("gt0")})
