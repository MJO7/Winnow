"""label: admin-style function. Actions:
  {"action": "migrate"}                -> apply pending SQL migrations to RDS
  {"action": "label", "repos": [...]}  -> run the re-run join for each repo (default: all)
"""
from __future__ import annotations

from common import configure_db_env, github_token, repos as default_repos
from winnow.db import get_conn, get_or_create_repo
from winnow.github_client import GitHubClient
from winnow.label import label_repo
from winnow.migrate import apply_migrations
from winnow.repos import get_repo


def handler(event, context):
    configure_db_env()
    event = event or {}
    action = event.get("action", "label")

    if action == "migrate":
        import os
        n = apply_migrations(os.environ["DATABASE_URL"], quiet=True)
        return {"applied": n}

    client = GitHubClient(token=github_token())
    out = {}
    with get_conn() as conn:
        for full in event.get("repos") or default_repos():
            spec = get_repo(full)
            repo_id = get_or_create_repo(conn, spec.owner, spec.name, spec.default_branch, spec.has_external_flaky_signal)
            s = label_repo(client, conn, repo_id, spec)
            out[full] = {"flake": s.flake, "real": s.real, "infra": s.infra, "excluded": s.excluded_by_rule}
    return out
