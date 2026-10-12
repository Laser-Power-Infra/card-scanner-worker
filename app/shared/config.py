"""Shared .env loader for the worker features.

Each feature keeps its own ``config.py`` that reads the variables it needs;
this module only provides the one thing both features duplicated: loading a
simple ``KEY=VALUE`` ``.env`` file from the repo root into ``os.environ``.
"""
import os
from pathlib import Path

# app/shared/config.py -> parents[2] is the repository root.
BASE_DIR = Path(__file__).resolve().parents[2]


def load_dotenv():
    """Populate os.environ from the repo-root .env without overriding real env."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
