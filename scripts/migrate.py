#!/usr/bin/env python3
"""Apply pending SQL migrations to DATABASE_URL. See winnow.migrate."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.config import load_config
from winnow.migrate import apply_migrations


if __name__ == "__main__":
    n = apply_migrations(load_config().database_url)
    print(f"Applied {n} migration(s)." if n else "No pending migrations.")
