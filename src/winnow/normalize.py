"""Deterministic failure-signature extraction from a raw CI job log.

The corpus (pytorch, home-assistant, airflow, cpython, pandas) is entirely
Python, so this targets pytest short-summary lines and Python tracebacks
specifically rather than a multi-language grammar. That's a real scope
narrowing, not an oversight -- a general-purpose log parser is a much
bigger, blurrier problem than "parse the two or three shapes Python test
failures actually take in CI output", and the narrower version is what
actually gets built and measured.

Precedence, most to least reliable:
  1. pytest's "short test summary info" lines (FAILED/ERROR <nodeid> - ...)
     -- compact, structured, and pytest emits exactly one per failing test.
  2. A Python traceback block (Traceback (most recent call last): ... )
     -- less structured, more free text, but still has a clear grammar.
  3. Tail fallback -- last non-empty lines of the log. Catches process
     kills, timeouts, "Process completed with exit code N" -- failures
     that never produced a Python-level exception at all.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_PYTEST_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)\s+-\s+(.*)$")
_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):\s*$")
_TRACEBACK_FRAME = re.compile(r'^\s*File "([^"]+)", line \d+, in (\S+)\s*$')
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt)):\s?(.*)$")

# --- generalization substitutions applied to message text --------------

_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b0x[0-9a-fA-F]{4,}\b"), "<ADDR>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<TS>"),
    (re.compile(r"/tmp/\S+"), "<TMP>"),
    (re.compile(r"/private/var/folders/\S+"), "<TMP>"),
    (re.compile(r"/var/folders/\S+"), "<TMP>"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\AppData\\Local\\Temp\\\S+"), "<TMP>"),
    (re.compile(r"/home/runner/work/[^/\s]+/[^/\s]+"), "<WORKDIR>"),
    (re.compile(r"/home/[^/\s]+"), "<HOME>"),
    (re.compile(r"/Users/[^/\s]+"), "<HOME>"),
    (re.compile(r"\b\d+:\d{2}:\d{2}(?:\.\d+)?\b"), "<DUR>"),
    (re.compile(r"\b\d+(?:\.\d+)?m\d+(?:\.\d+)?s\b"), "<DUR>"),
    (re.compile(r"\b\d+(?:\.\d+)?s\b"), "<DUR>"),
    (re.compile(r"\bpid[= ]\d+\b", re.IGNORECASE), "pid=<NUM>"),
    (re.compile(r"\bport[= ]\d+\b", re.IGNORECASE), "port=<NUM>"),
    (re.compile(r"\b\d{5,}\b"), "<NUM>"),
]


@dataclass
class Frame:
    file: str
    function: str

    def as_dict(self) -> dict:
        return {"file": self.file, "function": self.function}


@dataclass
class FailureSignature:
    signature_source: str  # pytest_failed_summary | traceback_block | tail_fallback
    exception_type: str | None
    message_skeleton: str
    top_frames: list[Frame] = field(default_factory=list)
    test_nodeids: list[str] = field(default_factory=list)

    @property
    def signature_text(self) -> str:
        frames = "; ".join(f"{f.file}:{f.function}" for f in self.top_frames)
        return f"{self.exception_type or ''} | {self.message_skeleton} | frames={frames}"

    @property
    def signature_hash(self) -> str:
        return hashlib.sha256(self.signature_text.encode("utf-8")).hexdigest()[:32]

    @property
    def retrieval_text(self) -> str:
        """What retrievers see: exception type + message only. Test ids and
        frames are held out as the relevance key, so retrieval has to work
        from the failure's description rather than its name."""
        prefix = f"{self.exception_type}: " if self.exception_type else ""
        return f"{prefix}{self.message_skeleton}"

    @property
    def failure_key(self) -> str | None:
        """Identity used as retrieval ground truth: 'the same test failed'
        (pytest, parametrization stripped) or 'the same exception at the
        same frame' (bare traceback). Tail-fallback signatures have no
        defensible identity and are excluded from the query set."""
        if self.signature_source == "pytest_failed_summary" and self.test_nodeids:
            return "test:" + self.test_nodeids[0].split("[", 1)[0]
        if self.signature_source == "traceback_block" and self.top_frames:
            f = self.top_frames[0]
            return f"exc:{self.exception_type}@{f.file}:{f.function}"
        return None


def _strip_lines(raw_log: str) -> list[str]:
    lines = raw_log.splitlines()
    out = []
    for line in lines:
        line = _TS_PREFIX.sub("", line)
        line = _ANSI.sub("", line)
        out.append(line)
    return out


def _normalize_text(text: str) -> str:
    for pattern, repl in _SUBS:
        text = pattern.sub(repl, text)
    return text.strip()


def _shorten_path(path: str) -> str:
    """Keeps the part of a path that's stable across machines/checkouts:
    everything after 'site-packages/' if present, else after the repo
    working-dir prefix, else just the basename chain of the last 2 parts.
    """
    for marker in ("site-packages/", "dist-packages/"):
        if marker in path:
            return path.split(marker, 1)[1]
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else path


def _extract_pytest_summary(lines: list[str]) -> FailureSignature | None:
    node_ids: list[str] = []
    first_exc_type: str | None = None
    first_message: str | None = None

    for line in lines:
        m = _PYTEST_SUMMARY_LINE.match(line)
        if not m:
            continue
        nodeid, rest = m.group(1), m.group(2)
        node_ids.append(nodeid)
        if first_message is None:
            exc_m = _EXCEPTION_LINE.match(rest)
            if exc_m:
                first_exc_type, first_message = exc_m.group(1), exc_m.group(2)
            else:
                first_exc_type, first_message = None, rest

    if not node_ids:
        return None

    return FailureSignature(
        signature_source="pytest_failed_summary",
        exception_type=first_exc_type,
        message_skeleton=_normalize_text(first_message or ""),
        top_frames=[Frame(file=_shorten_path(node_ids[0].split("::")[0]), function=node_ids[0].split("::")[-1])],
        test_nodeids=node_ids,
    )


def _extract_traceback_block(lines: list[str]) -> FailureSignature | None:
    # Find the LAST traceback block -- chronologically closest to the
    # point the job actually died, which is usually the causal one when
    # a job fails outside pytest's own per-test capture (e.g. a bare
    # script crash, an import-time error, a fixture teardown crash).
    starts = [i for i, l in enumerate(lines) if _TRACEBACK_START.match(l)]
    if not starts:
        return None
    start = starts[-1]

    frames: list[Frame] = []
    exc_type: str | None = None
    message = ""
    i = start + 1
    n = len(lines)
    while i < n:
        line = lines[i]
        fm = _TRACEBACK_FRAME.match(line)
        if fm:
            frames.append(Frame(file=_shorten_path(fm.group(1)), function=fm.group(2)))
            i += 1
            continue
        em = _EXCEPTION_LINE.match(line)
        if em:
            exc_type, message = em.group(1), em.group(2)
            break
        if line.strip() == "" and frames:
            # blank line right after frames with no exception line yet:
            # keep scanning, some formatters insert blank lines
            i += 1
            continue
        if not line.startswith((" ", "\t")) and line.strip() and frames:
            # A non-indented, non-exception line ends the block (e.g. the
            # next log section started) without a matched exception line.
            break
        i += 1

    if not frames and not exc_type:
        return None

    # Closest-to-error frames first (they appear last in the traceback).
    top_frames = list(reversed(frames))[:3]

    return FailureSignature(
        signature_source="traceback_block",
        exception_type=exc_type,
        message_skeleton=_normalize_text(message),
        top_frames=top_frames,
        test_nodeids=[],
    )


def _tail_fallback(lines: list[str], n: int = 5) -> FailureSignature:
    non_empty = [l for l in lines if l.strip()]
    tail = non_empty[-n:]
    return FailureSignature(
        signature_source="tail_fallback",
        exception_type=None,
        message_skeleton=_normalize_text(" | ".join(tail)),
        top_frames=[],
        test_nodeids=[],
    )


def extract_signature(raw_log: str) -> FailureSignature:
    lines = _strip_lines(raw_log)

    sig = _extract_pytest_summary(lines)
    if sig is not None:
        return sig

    sig = _extract_traceback_block(lines)
    if sig is not None:
        return sig

    return _tail_fallback(lines)
