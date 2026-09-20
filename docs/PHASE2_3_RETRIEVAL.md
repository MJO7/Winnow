# Retrieval: lexical baseline vs pgvector (k=5)

Query set: **434** failures with at least one prior same-`failure_key` occurrence in the same repo (363 with an exact-text prior duplicate, 71 without). Query text is exception type + message skeleton; the test name is never in the query.

## Overall

| Retriever | recall@5 | MRR | hit@5 | p50 ms | p95 ms |
|---|---|---|---|---|---|
| `fulltext_tsrank` | 0.981 | 0.986 | 0.995 | 1.05 | 1.76 |
| `trigram_similarity` | 0.985 | 0.987 | 0.995 | 4.0 | 8.23 |
| `pgvector_hnsw` | 0.985 | 0.989 | 0.995 | 11.35 | 20.11 |

## By stratum (MRR)

| Retriever | exact-dup prior exists | no exact-dup prior |
|---|---|---|
| `fulltext_tsrank` | 0.997 (n=363) | 0.928 (n=71) |
| `trigram_similarity` | 0.997 (n=363) | 0.937 (n=71) |
| `pgvector_hnsw` | 0.997 (n=363) | 0.947 (n=71) |

## By repo (MRR)

| Repo | `fulltext_tsrank` | `trigram_similarity` | `pgvector_hnsw` |
|---|---|---|---|
| apache/airflow | 1.000 | 1.000 | 1.000 |
| home-assistant/core | 0.900 | 0.900 | 0.900 |
| pandas-dev/pandas | 0.985 | 0.985 | 0.985 |
| python/cpython | 1.000 | 1.000 | 1.000 |
| pytorch/pytorch | 0.990 | 0.992 | 0.994 |

## Head to head: `fulltext_tsrank` vs `pgvector_hnsw` (per-query reciprocal rank)

| Stratum | baseline wins | challenger wins | tie |
|---|---|---|---|
| exact_dup | 0 | 0 | 363 |
| no_exact_dup | 2 | 4 | 65 |

### By exception type (n >= 5), sorted by where the baseline's edge is largest

| Exception type | n | baseline MRR | challenger MRR | baseline wins | challenger wins |
|---|---|---|---|---|---|
| `RuntimeError` | 211 | 0.992 | 0.991 | 2 | 2 |
| `torch._dynamo.exc.InternalTorchDynamoError` | 13 | 1.000 | 1.000 | 0 | 0 |
| `torch.distributed.elastic.multiprocessing.errors.ChildFailedError` | 5 | 1.000 | 1.000 | 0 | 0 |
| `TypeError` | 22 | 1.000 | 1.000 | 0 | 0 |
| `ModuleNotFoundError` | 11 | 1.000 | 1.000 | 0 | 0 |
| `SyntaxError` | 8 | 1.000 | 1.000 | 0 | 0 |
| `pandas.errors.Pandas4Warning` | 51 | 1.000 | 1.000 | 0 | 0 |
| `(none)` | 11 | 0.955 | 0.955 | 0 | 0 |
| `AssertionError` | 75 | 0.963 | 0.976 | 0 | 1 |
| `ImportError` | 16 | 0.927 | 0.969 | 0 | 1 |

### Example queries

**baseline wins** (job_id, repo, exception, baseline RR -> challenger RR):

- 97928572521 · pytorch/pytorch · `RuntimeError` · 1.00 -> 0.00
- 97994520738 · pytorch/pytorch · `RuntimeError` · 1.00 -> 0.00

**challenger wins** (job_id, repo, exception, baseline RR -> challenger RR):

- 97927767944 · pytorch/pytorch · `RuntimeError` · 0.33 -> 1.00
- 97961742817 · pytorch/pytorch · `AssertionError` · 0.00 -> 1.00
- 97961898616 · pytorch/pytorch · `RuntimeError` · 0.00 -> 1.00
- 97963398321 · pytorch/pytorch · `ImportError` · 0.33 -> 1.00
