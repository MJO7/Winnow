"""Triage generation (Phase 4): failure + retrieved neighbors -> structured
verdict with a citation, through a prompt-hash cache.

Separable from retrieval by construction: the caller hands in neighbor
job_ids from any Retriever, this module only renders and calls. The
citation is resolved by the eval, not trusted here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Literal, Protocol

import psycopg
from psycopg.types.json import Json
from pydantic import BaseModel, Field

logger = logging.getLogger("winnow.triage")

# Anthropic first-party rates, $/1M tokens, cached from the claude-api
# reference 2026-06-24. Cache reads billed at 0.1x input.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class TriageVerdict(BaseModel):
    classification: Literal["flake", "real", "infra"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(description="One line. Cite evidence from the prior failures, not general knowledge.")
    cited_run_id: int | None = Field(
        description="run_id of the ONE prior failure that best supports the classification, or null if none does."
    )


SYSTEM_PROMPT = """You triage CI failures for a software repository. You will be shown one failing job and up to K prior failures from the same repository that a retrieval system judged similar, each with what happened to it afterwards.

Classify the new failure:
- flake: the same test/job has previously failed and then passed on re-run with no code change. The prior evidence shows re-run success.
- real: a genuine regression. Prior similar failures were only fixed by a later commit, or there is no history of this failing and passing on re-run.
- infra: a GitHub runner / container / network / registry problem, not the test.

Rules:
- Base the verdict on the prior failures shown. If none of them is actually the same failure, say so in the rationale and lower your confidence.
- cited_run_id must be the run_id of one of the prior failures shown, or null. Never invent one.
- confidence is your probability that the classification is correct."""


@dataclass
class Neighbor:
    job_id: int
    run_id: int
    retrieval_text: str
    test_nodeids: list[str]
    label: str | None           # flake | real | infra | None (unlabeled: PR run or unresolved)
    label_reason: str | None
    attempts_to_resolution: int | None


def load_neighbors(conn: psycopg.Connection, job_ids: list[int]) -> list[Neighbor]:
    if not job_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fs.job_id, j.run_id, fs.retrieval_text, fs.test_nodeids,
                   l.label, l.label_reason, l.attempts_to_resolution
            FROM failure_signatures fs
            JOIN jobs j ON j.job_id = fs.job_id
            LEFT JOIN labels l ON l.job_id = fs.job_id
            WHERE fs.job_id = ANY(%s)
            """,
            (job_ids,),
        )
        rows = {r[0]: r for r in cur.fetchall()}
    out = []
    for jid in job_ids:  # preserve retrieval rank order
        if jid in rows:
            _, run_id, text, nodeids, label, reason, attempts = rows[jid]
            out.append(Neighbor(jid, run_id, text, nodeids or [], label, reason, attempts))
    return out


def _describe_outcome(n: Neighbor) -> str:
    if n.label == "flake":
        return f"OUTCOME: passed on re-run at the same commit (attempt {n.attempts_to_resolution}) -> flaky"
    if n.label == "real":
        return "OUTCOME: never passed at that commit; fixed by a later commit -> real regression"
    if n.label == "infra":
        return "OUTCOME: a GitHub setup/lifecycle step failed -> infrastructure"
    return "OUTCOME: unknown (pull-request run or not yet resolved)"


def render_user_prompt(query_text: str, query_nodeids: list[str], neighbors: list[Neighbor]) -> str:
    lines = ["# New failure", "", query_text]
    if query_nodeids:
        lines += ["", "Failing tests: " + ", ".join(query_nodeids[:5])]
    lines += ["", f"# Prior similar failures (K={len(neighbors)})", ""]
    if not neighbors:
        lines.append("(none retrieved)")
    for i, n in enumerate(neighbors, 1):
        lines += [f"## Prior {i}: run_id={n.run_id}", n.retrieval_text]
        if n.test_nodeids:
            lines.append("Failing tests: " + ", ".join(n.test_nodeids[:5]))
        lines.append(_describe_outcome(n))
        lines.append("")
    return "\n".join(lines)


def prompt_hash(model: str, system: str, user: str) -> str:
    payload = json.dumps(
        {"model": model, "system": system, "user": user, "schema": TriageVerdict.model_json_schema()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ModelCall:
    verdict: TriageVerdict
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    latency_ms: float
    cached: bool


class ModelClient(Protocol):
    model: str

    def call(self, system: str, user: str) -> ModelCall: ...


class AnthropicTriageClient:
    def __init__(self, model: str, effort: str = "low"):
        import anthropic

        self.model = model
        self.effort = effort
        self._client = anthropic.Anthropic()

    def call(self, system: str, user: str) -> ModelCall:
        kwargs = dict(
            model=self.model,
            max_tokens=1024,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=TriageVerdict,
        )
        if self.model != "claude-haiku-4-5":
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.effort}
        t0 = time.perf_counter()
        resp = self._client.messages.parse(**kwargs)
        latency = (time.perf_counter() - t0) * 1000
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise RuntimeError(f"model returned no verdict (stop_reason={resp.stop_reason})")
        u = resp.usage
        return ModelCall(
            verdict=resp.parsed_output,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            latency_ms=latency,
            cached=False,
        )


def cached_call(conn: psycopg.Connection, client: ModelClient, system: str, user: str) -> ModelCall:
    h = prompt_hash(client.model, system, user)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT response_json, input_tokens, output_tokens, cache_read_tokens, latency_ms FROM llm_cache WHERE prompt_hash = %s",
            (h,),
        )
        row = cur.fetchone()
    if row:
        return ModelCall(TriageVerdict.model_validate(row[0]), row[1], row[2], row[3], row[4], cached=True)

    call = client.call(system, user)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO llm_cache (prompt_hash, model, response_json, input_tokens, output_tokens, cache_read_tokens, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (h, client.model, Json(call.verdict.model_dump()), call.input_tokens, call.output_tokens, call.cache_read_tokens, call.latency_ms),
        )
    conn.commit()
    return call


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> float:
    # usage.input_tokens already excludes cache-read tokens.
    inp, out = PRICING[model]
    return (input_tokens * inp + cache_read_tokens * inp * 0.1 + output_tokens * out) / 1_000_000
