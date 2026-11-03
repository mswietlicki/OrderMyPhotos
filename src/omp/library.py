"""The whole library as OneDrive lists it: the latest manifest (rclone lsjson, zstd) from the OneDrive
mirror. It gives every album with its photos' content hashes and capture times (from their file names),
without reading the photos. S11 uses it to tell which unsorted photos are already in an album and which
events overlap one, even for albums the sample doesn't contain.
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .config import PHOTO_EXT
from .inventory import logical_album, time_from_name

ALBUMS = re.compile(r"^Pictures/Albums/\d{4}/[^/]+$")
ALBUM_DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")


class Library:
    def __init__(self, albums: dict, by_hash: dict, names: list[str], owner: str | None = None,
                 folders: dict | None = None):
        self.albums = albums      # album path -> {"start", "end", "photos", "name", "times"}
        self.by_hash = by_hash    # hex quickxor -> [album paths]
        self.names = names        # album folder names, for naming style
        self.owner = owner        # the OneDrive account's display name: the library's owner
        self.folders = folders or {}  # every folder with photos -> number of photos (any place in the library)

    @classmethod
    def load(cls, manifests: Path, tz: str) -> "Library | None":
        files = sorted(manifests.glob("*.lsjson.zst")) if manifests.exists() else []
        if not files:
            return None
        import zstandard

        zone = ZoneInfo(tz)
        times, by_hash = defaultdict(list), defaultdict(set)
        counts = defaultdict(int)
        creators = defaultdict(int)
        folders = defaultdict(int)
        with open(files[-1], "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as raw:
            buf = b""
            while chunk := raw.read(1 << 20):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    line = line.strip().rstrip(b",")
                    if not line.startswith(b"{"):
                        continue
                    d = json.loads(line)
                    path = d["Path"]
                    who = (d.get("Metadata") or {}).get("created-by-display-name")
                    if who:
                        creators[who] += 1
                    if d.get("IsDir") or PurePosixPath(path).suffix.lower() not in PHOTO_EXT:
                        continue
                    folders[str(PurePosixPath(path).parent)] += 1
                    if not path.startswith("Pictures/Albums/"):
                        continue
                    album, _ = logical_album(path)
                    if not ALBUMS.match(album):
                        continue
                    counts[album] += 1
                    qx = (d.get("Hashes") or {}).get("quickxor")
                    if qx:
                        by_hash[qx].add(album)
                    t = time_from_name(PurePosixPath(path).name, zone)
                    if t:
                        times[album].append(t[1])
        albums = {}
        for album, n in counts.items():
            ts = sorted(times.get(album, []))
            name = PurePosixPath(album).name
            if len(ts) >= 3:
                # Robust range: ignore a stray old photo copied into the album.
                lo, hi = ts[len(ts) // 20], ts[-1 - len(ts) // 20]
            else:
                m = ALBUM_DATE.match(name)
                if not m:
                    continue
                y, mo, dd = int(m[1]), int(m[2]), int(m[3] or 1)
                try:  # "2018-2019 …" starts like a date but isn't one
                    lo = datetime(y, mo, dd, tzinfo=zone).timestamp()
                except ValueError:
                    continue
                hi = lo + (86400 if m[3] else 31 * 86400)
            albums[album] = {"start": lo, "end": hi, "photos": n, "name": name, "times": ts}
        return cls(albums, {k: sorted(v) for k, v in by_hash.items()},
                   sorted({a["name"] for a in albums.values()}), max(creators, key=creators.get) if creators else None,
                   dict(folders))

    def misplaced_albums(self) -> list[tuple[str, str, int]]:
        """Folders named like an album ("YYYY-MM-DD name", "YYYY-MM name") outside Pictures/Albums/<year>/,
        or under the wrong year: (folder, where it belongs, photos). The user's rule: they should be moved (P30).
        Not albums: day folders ("2013-10-19"), a device's day folders ("2009-09-18 <camera>" three times or
        more), and dated folders inside an album or a curated folder (Private, Other, Info, Places, People)."""
        named = {}
        for folder in self.folders:
            p = PurePosixPath(folder)
            m = ALBUM_DATE.match(p.name)
            if m:
                named[folder] = (m, p.name[m.end():].strip(" -_"))
        # A name repeated after the date in 3+ sibling folders is a device's day folders ("2009-09-18 <camera>").
        repeats = Counter((str(PurePosixPath(f).parent), rest.lower()) for f, (_, rest) in named.items() if rest)
        out = []
        for folder, (m, rest) in named.items():
            name, parent = PurePosixPath(folder).name, str(PurePosixPath(folder).parent)
            if not (1 <= int(m[2]) <= 12) or "/_archived" in folder or not rest:  # a day folder isn't an album
                continue
            if repeats[(parent, rest.lower())] >= 3 or not folder.startswith(("Pictures/", "Obrazy/")):
                continue
            if re.match(r"^Pictures/(Private|Other|Info|Places|People)/|^Pictures/Albums/\d{4}/[^/]+/", folder):
                continue  # private or curated on purpose; a sub-album of an album stays inside it
            target = f"Pictures/Albums/{m[1]}/{name}"
            if folder != target:
                out.append((folder, target, self.folders[folder]))
        return sorted(out)

    def album_at(self, ts: float, margin: float = 2 * 3600) -> str | None:
        """The album whose event was going on at this moment: its photos' time span (plus a margin)
        contains it. When several do, the one with a photo closest in time. A photo taken during an
        event or trip almost always belongs to it, whatever folder it's in (user, 2026-11-03)."""
        import bisect

        best, best_d = None, None
        for album, a in self.albums.items():
            if not (a["start"] - margin <= ts <= a["end"] + margin):
                continue
            times = a["times"]
            if times:
                k = bisect.bisect_left(times, ts)
                d = min(abs(times[j] - ts) for j in (k - 1, k) if 0 <= j < len(times))
            else:
                d = 0.0 if a["start"] <= ts <= a["end"] else margin
            if best_d is None or d < best_d:
                best, best_d = album, d
        return best

    def overlapping(self, start: float, end: float, margin: float = 3600) -> list[tuple[str, float]]:
        """Albums whose capture-time range overlaps [start, end], with the overlap in seconds."""
        out = []
        for album, a in self.albums.items():
            ov = min(end + margin, a["end"]) - max(start - margin, a["start"])
            if ov > 0:
                out.append((album, ov))
        return sorted(out, key=lambda x: -x[1])


def manifests_dir() -> Path:
    return Path(os.environ.get("OMP_MANIFESTS", "/manifests"))
