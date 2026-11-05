"""Build an OrderMyPhotos test set like s5-s7 (docs/sample-runs.md): whole days of the library, so events and
bursts are complete.

- the user's albums dated in the window (`Pictures/Albums/<year>/<YYYY-MM-DD …>/`), whole, with v1's
  `_archived/` photos put back into their album;
- every unsorted Samsung Gallery and WhatsApp photo named with one of those days.

Only photo files, as btrfs reflinks of the pristine snapshot (no extra space). The listing and the hashes
come from the newest OneDrive manifest. Next to the set, `state/` gets the file list and the ground truth: for
each unsorted photo, the user's albums anywhere in the library that hold the same bytes (for measuring only,
never given to the pipeline).

    python3 scripts/testset.py FROM TO                                  # counts only
    python3 scripts/testset.py FROM TO /srv/mimir/cache/photos/tests/s7  # builds s7/before and s7/state
"""
import collections
import glob
import json
import os
import re
import subprocess
import sys

MIRROR = "/srv/mimir/onedrive/pristine/mirror"
MANIFESTS = "/srv/mimir/onedrive/manifests"
PHOTO = re.compile(r"\.(jpe?g|heic|png|webp)$", re.I)
DAY = re.compile(r"(20[12]\d)(\d\d)(\d\d)[_-]")
ALBUM = re.compile(r"^Pictures/Albums/\d{4}/((\d{4}-\d\d-\d\d)[^/]*)/")
lo, hi = sys.argv[1], sys.argv[2]
dest = sys.argv[3] if len(sys.argv) > 3 else None

manifest = sorted(glob.glob(f"{MANIFESTS}/onedrive-*.lsjson.zst"))[-1]
lines = subprocess.run(["zstd", "-dc", manifest], capture_output=True, check=True).stdout.decode().splitlines()
files, unsorted, by_hash, albums = [], [], collections.defaultdict(set), collections.Counter()
for line in lines:
    line = line.strip().rstrip(",")
    if not line.startswith("{"):
        continue
    d = json.loads(line)
    if d.get("IsDir"):
        continue
    p, qx = d["Path"], (d.get("Hashes") or {}).get("quickxor")
    m = ALBUM.match(p)
    if m and qx:
        by_hash[qx].add(m[1])
    if not PHOTO.search(p) or not os.path.exists(f"{MIRROR}/{p}"):
        continue
    if m and lo <= m[2] <= hi:
        files.append(p)
        albums[m[1]] += 1
    elif p.startswith(("Obrazy/Samsung Gallery/", "Obrazy/WhatsApp Images/")):
        n = DAY.search(p.rsplit("/", 1)[-1])
        if n and lo <= f"{n[1]}-{n[2]}-{n[3]}" <= hi:
            unsorted.append((p, qx))
truth = {p: sorted(by_hash.get(qx, ())) for p, qx in unsorted}
files += [p for p, _ in unsorted]
print(f"{lo}..{hi}: {len(files)} photos ({os.path.basename(manifest)}); {sum(albums.values())} in {len(albums)} albums, "
      f"{len(unsorted)} unsorted: {dict(collections.Counter('/'.join(p.split('/')[:2]) for p, _ in unsorted))}")
print(f"unsorted photos whose bytes are in one of the user's albums: {sum(1 for v in truth.values() if v)}, "
      f"in a test album: {sum(1 for v in truth.values() if set(v) & set(albums))}")
if not dest:
    sys.exit()
before, state = f"{dest}/before", f"{dest}/state"
if os.path.exists(before):
    sys.exit(f"{before} exists")
os.makedirs(state)
with open(f"{state}/files.txt", "w") as f:
    f.write("\n".join(sorted(files)) + "\n")
with open(f"{state}/truth.json", "w") as f:
    json.dump(truth, f, ensure_ascii=False, indent=0)
os.makedirs(before)
subprocess.run(["xargs", "-d", "\n", "cp", "--reflink=always", "--preserve=timestamps", "--parents", "-t",
                os.path.abspath(before)], input="\n".join(files).encode(), cwd=MIRROR, check=True)
moved = 0
for d in sorted(glob.glob(f"{before}/Pictures/Albums/*/*/**/_archived", recursive=True), key=len, reverse=True):
    album = os.path.dirname(d)
    for root, _, names in os.walk(d):
        for n in names:
            stem, ext = os.path.splitext(n)
            target = f"{album}/{n}" if not os.path.exists(f"{album}/{n}") else f"{album}/{stem}_archived{ext}"
            os.rename(f"{root}/{n}", target)
            moved += 1
    subprocess.run(["find", d, "-depth", "-type", "d", "-empty", "-delete"], check=True)
subprocess.run(["chmod", "-R", "g+rwX,o-rwx", dest], check=True)
n = sum(len(f) for _, _, f in os.walk(before))
print(f"built {before}: {n} photos, {moved} put back from _archived/")
