"""Each photo's nearest look-alikes across the whole library (P45): the top K by SigLIP 2 cosine, from any
folder, so that a photo's look-alikes are found whether they're in an album, a thematic folder or another
unsorted folder. S11 uses them as places to try and as evidence ("photos that look like these went to ...").

Stored in `neighbour` (one row per photo and look-alike) and built again when a photo has no list yet. On s6
(517 photos) that's one matrix product. The full library (~173,000 photos) needs it in chunks on the GPU, or
an index such as FAISS; a graph database isn't needed, because the queries go one or two steps deep.
"""

import numpy as np

from .db import get_vecs

K = 20                     # look-alikes kept per photo
SCHEMA = """CREATE TABLE IF NOT EXISTS neighbour (
  id TEXT NOT NULL, other TEXT NOT NULL, kind TEXT NOT NULL, sim REAL NOT NULL,  -- kind: look (SigLIP 2)
  PRIMARY KEY (id, kind, other)
);"""


def build(con, k: int = K, chunk: int = 1024) -> int:
    """Each photo's top-k look-alikes; rebuilt for every photo when one has none (a new photo can be anyone's
    look-alike). Returns the number of photos it was built for, 0 when nothing changed."""
    con.executescript(SCHEMA)
    vecs = get_vecs(con, "siglip")
    ids = sorted(vecs)
    have = {r[0] for r in con.execute("SELECT DISTINCT id FROM neighbour WHERE kind='look'")}
    if len(ids) < 2 or not set(ids) - have:
        return 0
    mat = np.stack([vecs[i] for i in ids])
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    k = min(k, len(ids) - 1)
    con.execute("DELETE FROM neighbour WHERE kind='look'")
    for s in range(0, len(ids), chunk):
        sims = mat[s:s + chunk] @ mat.T
        sims[np.arange(len(sims)), np.arange(s, s + len(sims))] = -1.0  # not its own look-alike
        top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        con.executemany("INSERT INTO neighbour(id, other, kind, sim) VALUES (?,?,'look',?)",
                        [(ids[s + n], ids[j], float(sims[n, j])) for n in range(len(sims)) for j in top[n]])
    con.commit()
    return len(ids)


def look_alikes(con, min_sim: float) -> dict[str, list[tuple[str, float]]]:
    """{photo: [(look-alike, cosine), ...]} at least min_sim alike, most alike first."""
    out = {}
    for r in con.execute("SELECT id, other, sim FROM neighbour WHERE kind='look' AND sim >= ? ORDER BY id, sim DESC",
                         (min_sim,)):
        out.setdefault(r[0], []).append((r[1], r[2]))
    return out
