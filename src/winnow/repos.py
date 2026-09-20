"""The five-repository corpus. Selected 2026-09-19 against the criteria in
the spec: GitHub Actions native, >200 runs/week, Python test suites with
visible test names (verified: all five are pure-Python or Python-suite
projects, so Phase 1's normalizer can target pytest/unittest tracebacks
specifically instead of a multi-language grammar), and at least one repo
with an independent flakiness signal.

Verified by hand via the public API on 2026-09-19 (unauthenticated, so
log-download and exact retention-window checks were NOT possible --
GitHub's job-logs endpoint 403s without a token regardless of repo
visibility. That verification is pending GITHUB_TOKEN; see README).
Confirmed via `GET /repos/{owner}/{repo}/actions/runs`: all five return
many dozens of runs within a single-hour window, comfortably clearing the
200/week bar.

pytorch/pytorch is the external-validation repo: its "DISABLED test_x
(module.Class)" issues, labeled `module: flaky-tests`, are auto-filed by
PyTorch's own flaky-test bot. Confirmed live: 6,100+ open issues matching
that pattern as of 2026-09-19 via the search API.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoSpec:
    owner: str
    name: str
    default_branch: str
    has_external_flaky_signal: bool
    notes: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


CORPUS: list[RepoSpec] = [
    RepoSpec(
        owner="pytorch",
        name="pytorch",
        default_branch="main",
        has_external_flaky_signal=True,
        notes=(
            "External validation repo. Flaky-test bot auto-files "
            "'DISABLED test_x (Class)' issues labeled 'module: flaky-tests'. "
            "Very high CI volume (dozens of runs/hour); matrix legs across "
            "CUDA/ROCm/CPU and multiple Python versions."
        ),
    ),
    RepoSpec(
        owner="home-assistant",
        name="core",
        default_branch="dev",
        has_external_flaky_signal=False,
        notes=(
            "Large pytest suite, visible test node ids, matrix across "
            "Python versions and service container versions (e.g. mariadb)."
        ),
    ),
    RepoSpec(
        owner="apache",
        name="airflow",
        default_branch="main",
        has_external_flaky_signal=False,
        notes="Large test matrix, known history of flaky tests, GH Actions native.",
    ),
    RepoSpec(
        owner="python",
        name="cpython",
        default_branch="main",
        has_external_flaky_signal=False,
        notes="unittest-based, matrix across OS/arch, GH Actions native for PR/push CI.",
    ),
    RepoSpec(
        owner="pandas-dev",
        name="pandas",
        default_branch="main",
        has_external_flaky_signal=False,
        notes="pytest-based, matrix across OS and NumPy/Python versions.",
    ),
]


def get_repo(full_name: str) -> RepoSpec:
    for r in CORPUS:
        if r.full_name == full_name:
            return r
    raise KeyError(f"{full_name} is not in the Winnow corpus: {[r.full_name for r in CORPUS]}")
