"""Sessions and phases of the web app (P47).

A session is one pass of OrderMyPhotos over one root directory: the whole OneDrive library, a test set,
later a drive. It has its own DB, status, log and review state in `<state>/sessions/<id>/`, and its cache
and review bundle in `<cache>/<id>/`. One session is active at a time, and a new one can only be made
active while the current one isn't running.

The work is a fixed sequence of phases. A server phase runs pipeline stages; a review phase waits for the
reviewer. Start runs the server phases one after the other and stops at the next review; the reviewer's
"Done" lets the run go on. Photos the reviewer takes out of an album are sorted again by the `resort`
phase, and then reviewed again, as often as needed, without redoing what was already confirmed.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

STATE = Path(os.environ.get("OMP_STATE_DIR", "/state"))
CACHE = Path(os.environ.get("OMP_SESSIONS_CACHE", "/cache"))
# The directories a session's root may be in. In the container they're mounted read-only at the same
# paths as on the host, so a root is typed or picked as the host path.
ROOTS = [Path(p) for p in os.environ.get("OMP_ROOTS", "").split(":") if p]
SESSIONS = STATE / "sessions"
ACTIVE = STATE / "active.json"

PHASES = [
    {"key": "read", "kind": "server", "title": "Read the photos",
     "about": "Find every photo, hash it, read its metadata and make the previews; save them to the library catalog.",
     "stages": ["inventory", "previews", "catalog"]},
    {"key": "signals", "kind": "server", "title": "Look at every photo",
     "about": "Embeddings, quality, orientation, faces, people and animals, depth, text pages.",
     "stages": ["signals", "orient", "faces", "subjects", "depth", "text"],
     "lanes": True},  # side by side, one lane per device (lanes.py, P51)
    {"key": "people", "kind": "server", "title": "Find people and pets",
     "about": "Group the faces and the animals, so they can be named.", "stages": ["people"]},
    {"key": "names", "kind": "review", "title": "Name people and pets", "page": "people",
     "about": "Name the people and pets you know, merge the cards of one person, mark yourself."},
    {"key": "picks", "kind": "server", "title": "Bursts and picks",
     "about": "Apply the names, then group bursts and pick the frames to keep.",
     "stages": ["people", "place", "group", "rank", "judge"]},
    {"key": "sort", "kind": "server", "title": "Describe and sort",
     "about": "Captions with the names, then a place for every photo: an album or a folder.",
     "stages": ["captions", "albums"]},
    {"key": "review", "kind": "review", "title": "Review the albums", "page": "review",
     "about": "Check each album and folder: the frames kept and archived, and the photos sorted into it."},
    {"key": "resort", "kind": "server", "title": "Sort the photos taken out",
     "about": "Find another place for the photos you took out of an album.", "stages": ["albums"]},
    {"key": "plan", "kind": "server", "title": "Make the plan",
     "about": "Every move and archive the review leads to. The sorting runs once more first, to apply what you "
              "confirmed, renamed or moved; it touches nothing else, since everything is confirmed by then.",
     "stages": ["albums", "plan"]},
    {"key": "final", "kind": "review", "title": "Final decision", "page": "final",
     "about": "Check the plan, and decide what happens to the archived photos."},
    {"key": "apply", "kind": "server", "title": "Apply the plan",
     "about": "Move the photos as planned, turn them upright, and delete the archived ones if you chose so. A test "
              "session changes only its own copy; the OneDrive library's plan goes to OneDrive with "
              "mimir-onedrive apply.",
     "stages": ["apply"]},
]
PHASE = {p["key"]: i for i, p in enumerate(PHASES)}


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "session"


def allowed(root: Path) -> bool:
    root = root.resolve()
    return any(root == b.resolve() or root.is_relative_to(b.resolve()) for b in ROOTS)


@dataclass
class Session:
    id: str
    name: str
    root: str
    library_index: str = "self"      # manifest: S11 knows the whole OneDrive library; self: only this root
    created: float = field(default_factory=time.time)
    run: str | None = None           # the run id its DB rows carry, when it isn't the session's id (an adopted DB)
    # The user said this storage may hold photos another storage already keeps (a backup). Otherwise such a copy is
    # archived here as a duplicate: one photo, one storage (P58).
    keep_duplicates: bool = False

    @property
    def run_id(self) -> str:
        return self.run or self.id

    @property
    def dir(self) -> Path:
        return SESSIONS / self.id

    @property
    def cache_dir(self) -> Path:
        return CACHE / self.id

    def config(self) -> Config:
        return Config(root=Path(self.root), db=self.dir / "omp.db", cache=self.cache_dir / "cache",
                      bundle=self.cache_dir / "bundle")

    def env(self) -> dict:
        """The environment the pipeline runs with: everything it reads comes from here."""
        c = self.config()
        return {"OMP_ROOT": str(c.root), "OMP_DB": str(c.db), "OMP_CACHE": str(c.cache), "OMP_BUNDLE": str(c.bundle),
                "OMP_STATUS": str(self.dir / "status.json"), "OMP_LIBRARY_INDEX": self.library_index,
                "OMP_RUN": self.run_id, "OMP_KEEP_DUPLICATES": "1" if self.keep_duplicates else "0"}

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for d in ("cache", "bundle"):
            (self.cache_dir / d).mkdir(parents=True, exist_ok=True)
        _write(self.dir / "session.json", asdict(self))


def _write(path: Path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load(id_: str) -> Session | None:
    d = _read(SESSIONS / id_ / "session.json", None)
    return Session(**d) if d else None


def all_sessions() -> list[Session]:
    if not SESSIONS.exists():
        return []
    return sorted((s for d in SESSIONS.iterdir() if (s := load(d.name))), key=lambda s: -s.created)


def active() -> Session | None:
    return load(_read(ACTIVE, {}).get("id", "")) if ACTIVE.exists() else None


def set_active(id_: str):
    STATE.mkdir(parents=True, exist_ok=True)
    _write(ACTIVE, {"id": id_, "since": time.time()})


def create(name: str, root: str, library_index: str | None = None, keep_duplicates: bool = False) -> Session:
    path = Path(root)
    if not path.is_absolute() or not allowed(path):
        raise ValueError(f"the root must be inside one of {', '.join(map(str, ROOTS)) or '(no roots configured)'}")
    if not path.is_dir():
        raise ValueError(f"{root} is not a directory")
    base, n = slug(name), 1
    id_ = base
    while (SESSIONS / id_).exists():
        n += 1
        id_ = f"{base}-{n}"
    onedrive = str(path.resolve()).startswith("/srv/mimir/onedrive")
    s = Session(id=id_, name=name.strip() or id_, root=str(path),
                library_index=library_index or ("manifest" if onedrive else "self"), keep_duplicates=bool(keep_duplicates))
    s.save()
    return s


def browse(path: str | None) -> dict:
    """The folders under one of the allowed roots, for picking a session's root."""
    if not path:
        return {"path": None, "dirs": [str(b) for b in ROOTS if b.is_dir()]}
    p = Path(path)
    if not allowed(p) or not p.is_dir():
        raise ValueError(f"{path} isn't a folder inside an allowed root")
    dirs = sorted(d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith("."))
    photos = sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".heic", ".png"))
    parent = str(p.parent) if allowed(p.parent) and p.parent != p else None
    return {"path": str(p), "parent": parent, "dirs": dirs, "photos_here": photos}
