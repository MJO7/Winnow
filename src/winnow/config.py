from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Config:
    github_token: str | None
    database_url: str
    log_dir: Path


def load_config() -> Config:
    """Loads .env (if present) then reads env vars. github_token may be
    None -- callers that need authenticated-only endpoints (job logs) must
    check and fail with a clear message rather than silently going
    unauthenticated at 60 req/hr.
    """
    load_dotenv(_PROJECT_ROOT / ".env", override=False)

    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://winnow:winnow_dev_local_only@localhost:5433/winnow",
    )
    log_dir = Path(os.environ.get("WINNOW_LOG_DIR", "data/logs"))
    if not log_dir.is_absolute():
        log_dir = _PROJECT_ROOT / log_dir

    return Config(
        github_token=os.environ.get("GITHUB_TOKEN") or None,
        database_url=database_url,
        log_dir=log_dir,
    )
