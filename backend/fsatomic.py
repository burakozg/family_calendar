"""Atomic file writes — the one primitive both storage and the activity log
need (kept separate so neither has to import the other)."""
import os
from pathlib import Path


def _atomic_write_text(path: Path, text: str):
    """Write via a temp file + atomic rename so a crash or power loss can never
    leave a torn/half-written file — these JSONs are the only copy of the data."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
