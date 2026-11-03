"""SQLite state: one database per library (or per sample), WAL mode.

A photo is identified by `id` = the hex QuickXorHash of its bytes (what rclone lists as `quickxor`),
so moves and renames on OneDrive don't change it. Paths live in `file`; everything computed lives in
tables keyed by `id`, with the model and version that produced it.
"""

import json
import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS file (
  path TEXT PRIMARY KEY,           -- relative to the library root, as on OneDrive
  id TEXT NOT NULL,                -- hex QuickXorHash
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  media TEXT NOT NULL,             -- photo | video | other
  policy TEXT NOT NULL,            -- cull | unsorted | describe | ignore
  album TEXT,                      -- logical album folder (v1's _archived/ maps back to its album)
  v1_archived INTEGER NOT NULL DEFAULT 0,
  seen_run TEXT
);
CREATE INDEX IF NOT EXISTS file_id ON file(id);

CREATE TABLE IF NOT EXISTS photo (
  id TEXT PRIMARY KEY,
  taken_local TEXT,                -- ISO local wall time, with sub-seconds when known
  taken_ts REAL,                   -- UTC epoch seconds
  time_source TEXT,                -- exif | filename | gps | mtime
  utc_offset TEXT,
  make TEXT, model TEXT, lens TEXT,
  iso REAL, exposure REAL, fnumber REAL, focal REAL, focal35 REAL,
  lat REAL, lon REAL, alt REAL,
  width INTEGER, height INTEGER,   -- after EXIF orientation
  orientation INTEGER,
  motion_photo INTEGER NOT NULL DEFAULT 0,
  meta TEXT                        -- JSON: existing title/description/keywords with provenance, raw extras
);

CREATE TABLE IF NOT EXISTS signal (
  id TEXT NOT NULL,
  kind TEXT NOT NULL,              -- e.g. dino, siglip, phash, sharp, exposure, iqa, depth
  model TEXT NOT NULL,             -- model@version that produced it
  value REAL,
  vec BLOB,                        -- float16 vector or grid
  shape TEXT,                      -- JSON shape of vec
  data TEXT,                       -- JSON extras
  created TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (id, kind)
);

CREATE TABLE IF NOT EXISTS face (
  fid INTEGER PRIMARY KEY,
  id TEXT NOT NULL,
  model TEXT NOT NULL,
  box TEXT NOT NULL,               -- JSON [x0,y0,x1,y1], normalized to the oriented image
  score REAL, size REAL,           -- detector score; face height / image height
  yaw REAL, pitch REAL, roll REAL,
  eye_l REAL, eye_r REAL,          -- eye openness (height/width of the eye contour)
  sharp REAL,                      -- Tenengrad on the full-resolution face crop
  emb BLOB,                        -- 512-d identity embedding, float16
  person INTEGER
);
CREATE INDEX IF NOT EXISTS face_photo ON face(id);

CREATE TABLE IF NOT EXISTS grp (
  gid INTEGER PRIMARY KEY,
  run TEXT NOT NULL,
  kind TEXT NOT NULL,              -- copy | burst | event
  cohesion REAL,                   -- mean pairwise margin over the similarity curve
  data TEXT
);
CREATE TABLE IF NOT EXISTS grp_member (
  gid INTEGER NOT NULL, id TEXT NOT NULL, ord INTEGER,
  PRIMARY KEY (gid, id)
);

CREATE TABLE IF NOT EXISTS decision (
  id TEXT NOT NULL,
  run TEXT NOT NULL,
  gid INTEGER,                     -- burst, or NULL for a single
  verdict TEXT NOT NULL,           -- keep | archive
  rank INTEGER,
  score REAL,
  floor TEXT,                      -- NULL, or why it fails the quality floor
  protected TEXT,                  -- why it may not be archived in bulk
  tier TEXT NOT NULL,              -- auto | review
  reasons TEXT,                    -- JSON list of plain-language reasons
  features TEXT,                   -- JSON per-feature values used
  PRIMARY KEY (id, run)
);

CREATE TABLE IF NOT EXISTS judge (
  a TEXT NOT NULL, b TEXT NOT NULL, model TEXT NOT NULL,
  p_a REAL,                        -- P(a better), averaged over both orders
  p_ab REAL, p_ba REAL,            -- raw P(first is better) per order
  data TEXT,
  PRIMARY KEY (a, b, model)
);

CREATE TABLE IF NOT EXISTS caption (
  id TEXT NOT NULL, model TEXT NOT NULL,
  data TEXT NOT NULL,              -- the JSON the VLM returned, plus mapped tags
  seconds REAL,
  PRIMARY KEY (id, model)
);

CREATE TABLE IF NOT EXISTS v1_burst (
  album TEXT NOT NULL, first_file TEXT NOT NULL,
  kept TEXT, archived TEXT,        -- JSON lists of file names
  reasoning TEXT,
  PRIMARY KEY (album, first_file)
);

CREATE TABLE IF NOT EXISTS album_context (
  album TEXT NOT NULL, source TEXT NOT NULL,  -- context.txt (user) | _trip-context.txt | DESCRIPTION.md (machine)
  trusted INTEGER NOT NULL, text TEXT,
  PRIMARY KEY (album, source)
);

CREATE TABLE IF NOT EXISTS label (
  lid INTEGER PRIMARY KEY,
  id TEXT NOT NULL, kind TEXT NOT NULL,       -- burst | keep | bad | category
  value TEXT, source TEXT, created TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS timing (
  stage TEXT NOT NULL, run TEXT NOT NULL, items INTEGER, seconds REAL, device TEXT,
  created TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def put_signal(con, id_, kind, model, value=None, vec=None, data=None):
    blob = shape = None
    if vec is not None:
        arr = np.asarray(vec, dtype=np.float16)
        blob, shape = arr.tobytes(), json.dumps(list(arr.shape))
    con.execute(
        "INSERT OR REPLACE INTO signal(id, kind, model, value, vec, shape, data) VALUES (?,?,?,?,?,?,?)",
        (id_, kind, model, value, blob, shape, json.dumps(data) if data is not None else None),
    )


def get_vecs(con, kind) -> dict[str, np.ndarray]:
    out = {}
    for r in con.execute("SELECT id, vec, shape FROM signal WHERE kind=? AND vec IS NOT NULL", (kind,)):
        out[r["id"]] = np.frombuffer(r["vec"], dtype=np.float16).reshape(json.loads(r["shape"])).astype(np.float32)
    return out


def get_data(con, kind) -> dict[str, dict]:
    return {
        r["id"]: {"value": r["value"], **(json.loads(r["data"]) if r["data"] else {})}
        for r in con.execute("SELECT id, value, data FROM signal WHERE kind=?", (kind,))
    }


def done_ids(con, kind, model) -> set[str]:
    return {r[0] for r in con.execute("SELECT id FROM signal WHERE kind=? AND model=?", (kind, model))}


def log_timing(con, stage, run, items, seconds, device=None):
    con.execute(
        "INSERT INTO timing(stage, run, items, seconds, device) VALUES (?,?,?,?,?)",
        (stage, run, items, seconds, device),
    )
