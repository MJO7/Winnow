#!/usr/bin/env python3
"""Renders the Phase 1 exit-criterion tables as Markdown to stdout (or
--out FILE): per-repo label class counts, every exclusion rule with the
records it dropped, retry-config findings, and external-signal agreement.

    python scripts/report_phase1.py --out docs/PHASE1_RESULTS.md
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from winnow.db import get_conn
from winnow.exclusions import Rule
from winnow.external_signals import compute_agreement
from winnow.retry_scan import summarize as summarize_retry

RULE_EXPLANATIONS = {
    Rule.NON_PUSH_OR_NON_DEFAULT_BRANCH: (
        "Run is a pull_request/schedule/etc. event, or a push to a non-default branch. "
        "PR head SHAs don't pin the tested content (the checkout is a merge with a moving base), "
        "so 'same SHA re-ran and passed' is not a valid flake signal. Still ingested for retrieval."
    ),
    Rule.RUN_NOT_COMPLETED: "Run still queued/in progress at ingest time; outcome not yet knowable.",
    Rule.RUN_CANCELLED_SUPERSEDED: (
        "Whole run concluded 'cancelled' -- typically a concurrency group cancelling in-progress "
        "runs on a newer push. Not a test outcome."
    ),
    Rule.NO_RESOLUTION_WITHIN_INGEST_WINDOW: (
        "Job's last attempt failed and no later push-to-default-branch run of the same job succeeded "
        "inside the ingested window. Can't distinguish 'real, unfixed yet' from 'window too short'."
    ),
    Rule.FORCE_PUSH_OR_DIVERGED_HISTORY: (
        "A later run of the job succeeded, but the compare API reports its commit is not a "
        "descendant of the failing commit (diverged/behind) -- history was rewritten, so the "
        "'fixed by a later commit' inference doesn't hold."
    ),
    Rule.WORKFLOW_AUTORETRY_SUSPECTED: (
        "Workflow's attempt-to-attempt delay is short and near-uniform across >=5 runs "
        "(mean <= 5 min, CV <= 0.15): the signature of a bot auto-retrying, which inflates "
        "the flake class. All jobs in the workflow excluded."
    ),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    lines: list[str] = ["# Phase 1 results: derived labels and exclusions", ""]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, owner, name, has_external_flaky_signal FROM repos ORDER BY id")
            repos = cur.fetchall()

        for repo_id, owner, name, has_ext in repos:
            lines.append(f"## {owner}/{name}")
            lines.append("")

            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM workflow_runs WHERE repo_id = %s", (repo_id,))
                n_runs = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM jobs WHERE repo_id = %s", (repo_id,))
                n_jobs = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM jobs WHERE repo_id = %s AND conclusion = 'failure'", (repo_id,))
                n_failed = cur.fetchone()[0]
                cur.execute(
                    "SELECT log_fetch_status, COUNT(*) FROM jobs WHERE repo_id = %s AND conclusion = 'failure' GROUP BY 1",
                    (repo_id,),
                )
                log_status = dict(cur.fetchall())
                cur.execute("SELECT label, COUNT(*) FROM labels WHERE repo_id = %s GROUP BY 1 ORDER BY 1", (repo_id,))
                label_counts = dict(cur.fetchall())
                cur.execute("SELECT rule, COUNT(*) FROM exclusions WHERE repo_id = %s GROUP BY 1 ORDER BY 2 DESC", (repo_id,))
                excl = cur.fetchall()

            lines += [
                "| Corpus | Count |",
                "|---|---|",
                f"| Workflow runs ingested | {n_runs} |",
                f"| Job records | {n_jobs} |",
                f"| Failed jobs | {n_failed} |",
                f"| Failed-job logs fetched | {log_status.get('fetched', 0)} |",
                f"| Failed-job logs expired (410) | {log_status.get('expired_410', 0)} |",
                f"| Failed-job logs not attempted | {log_status.get('not_attempted', 0)} |",
                "",
                "| Label | Count |",
                "|---|---|",
                f"| flake | {label_counts.get('flake', 0)} |",
                f"| real | {label_counts.get('real', 0)} |",
                f"| infra | {label_counts.get('infra', 0)} |",
                "",
                "| Exclusion rule | Records dropped | Reason |",
                "|---|---|---|",
            ]
            seen = set()
            for rule, count in excl:
                seen.add(rule)
                lines.append(f"| `{rule}` | {count} | {RULE_EXPLANATIONS.get(rule, '')} |")
            for rule in Rule.ALL:
                if rule not in seen:
                    lines.append(f"| `{rule}` | 0 | {RULE_EXPLANATIONS[rule]} |")
            lines.append("")

            retry = summarize_retry(conn, repo_id)
            lines.append("**In-process retry markers in workflow YAML** (survivorship-bias check):")
            lines.append("")
            if retry:
                lines += ["| Marker | Occurrences |", "|---|---|"]
                lines += [f"| {m} | {c} |" for m, c in retry]
            else:
                lines.append("_none found, or scan not run_")
            lines.append("")

            if has_ext:
                a = compute_agreement(conn, repo_id)
                lines += [
                    "**Agreement with external flaky-test signal** (pytorch DISABLED-test bot):",
                    "",
                    "| Metric | Value |",
                    "|---|---|",
                    f"| Tests the bot has disabled as flaky | {a.external_flaky} |",
                    f"| Distinct tests in derived-flake jobs | {a.derived_flaky} |",
                    f"| Distinct tests in derived-real jobs | {a.derived_real} |",
                    f"| Overlap: derived flaky AND bot flaky | {a.overlap_flaky} |",
                    f"| Derived flaky, bot silent | {a.derived_flaky_not_external} |",
                    f"| Bot flaky, we called real | {a.external_flaky_seen_but_derived_real} |",
                    f"| Bot flaky, never observed failing in our window | {a.external_flaky_never_observed} |",
                    f"| Precision of derived flake vs bot | {_fmt(a.precision_of_derived_flake)} |",
                    f"| Recall of derived flake vs bot (over observed) | {_fmt(a.recall_of_derived_flake)} |",
                    "",
                ]
                if a.derived_flaky == 0 and a.derived_real == 0:
                    lines.append(
                        "_No per-test derived labels yet: agreement requires fetched + parsed failed-job logs "
                        "(`failure_signatures.test_nodeids`), which requires GITHUB_TOKEN._"
                    )
                    lines.append("")

    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


if __name__ == "__main__":
    main()
