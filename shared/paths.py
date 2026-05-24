from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "sources.yaml"
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
ARCHIVE_DIR = ROOT / "archives" / "research_reader"
PREFERENCES_PATH = CONFIG_DIR / "research_preferences.yaml"
DB_PATH = DATA_DIR / "reader.db"
DIGEST_PATH = OUTPUT_DIR / "digest.html"


def ensure_project_dirs() -> None:
    for path in [CONFIG_DIR, DATA_DIR, OUTPUT_DIR, ARCHIVE_DIR]:
        path.mkdir(parents=True, exist_ok=True)
