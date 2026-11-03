"""Paths and settings. Everything comes from environment variables, so the same image runs the sample
and the whole library. Defaults match the container layout in compose.yaml."""

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".mpo", ".cr2", ".dng", ".gif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".avi", ".mkv", ".3gp", ".m4v", ".wmv", ".mpg", ".mpeg"}
PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    root: Path = field(default_factory=lambda: Path(os.environ.get("OMP_ROOT", "/library")))
    db: Path = field(default_factory=lambda: Path(os.environ.get("OMP_DB", "/state/omp.db")))
    cache: Path = field(default_factory=lambda: Path(os.environ.get("OMP_CACHE", "/cache")))
    models: Path = field(default_factory=lambda: Path(os.environ.get("OMP_MODELS", "/models")))
    llm: str = field(default_factory=lambda: os.environ.get("OMP_LLM", "http://127.0.0.1:9292"))
    policy_file: Path = field(default_factory=lambda: Path(os.environ.get("OMP_POLICY", PACKAGE_DIR / "policy.yaml")))
    workers: int = field(default_factory=lambda: int(os.environ.get("OMP_WORKERS", "20")))
    tz: str = field(default_factory=lambda: os.environ.get("OMP_TZ", "Europe/Warsaw"))
    # The review bundle (S6) at the root of the library; for OneDrive it's built on Mimir and uploaded.
    bundle: Path = field(default_factory=lambda: Path(os.environ.get("OMP_BUNDLE", "/library/OrderMyPhotos")))

    def preview(self, id_: str) -> Path:
        return self.cache / "preview" / id_[:2] / f"{id_}.jpg"

    def thumb(self, id_: str) -> Path:
        return self.cache / "thumb" / id_[:2] / f"{id_}.jpg"

    def depth(self, id_: str) -> Path:
        return self.cache / "depth" / id_[:2] / f"{id_}.png"


class Policy:
    """Folder policy: the first rule whose glob matches the path wins."""

    def __init__(self, path: Path):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.rules = [(r["glob"], r["policy"]) for r in spec["rules"]]
        self.default = spec.get("default", "unsorted")
        self.precedence = spec.get("precedence", [])

    def __call__(self, rel: str) -> str:
        p = PurePosixPath(rel)
        for glob, policy in self.rules:
            if p.full_match(glob):
                return policy
        return self.default

    def rank(self, rel: str) -> int:
        """Lower is more curated: decides which exact copy is kept (S1)."""
        p = PurePosixPath(rel)
        for i, glob in enumerate(self.precedence):
            if p.full_match(glob):
                return i
        return len(self.precedence)
