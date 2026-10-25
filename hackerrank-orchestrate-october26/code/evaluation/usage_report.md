# Usage Report — Ghost Data Still Exists

## Overview

This report summarizes the final full-dataset run that produced `output.csv`
for the HackerRank Orchestrate (October 2026) challenge: **Ghost Data Still
Exists**.

## Solution Architecture

The agent is implemented entirely in Python using the standard library only
(`csv`, `json`, `sqlite3`, `re`, `pathlib`, `datetime`). It is organized into
three layers:

| Module | Purpose |
|---|---|
| `data_index.py` | Loads all 6 participant-facing CSVs; builds identity graph, reverse identifier maps, rule lookups, activity indexes, evidence manifest |
| `evidence_parser.py` | Parses evidence files (SQL, JSON, TXT/LOG, PDF, image metadata); builds identifier and resource indexes |
| `audit_engine.py` | Deletion authorization, state reconstruction at `audit_at`, rule application, ordered step generation |
| `main.py` | Entry point: processes all 518 cases and writes `output.csv` |

Optional PDF/image parsing uses `pdfplumber`, `PyPDF2`, and `Pillow` when
available, but the core logic requires no third-party packages.

## Model Usage

**No ML/LM models were used for output generation.**

The deletion-audit agent is a deterministic, rule-based system. It does not
make any model calls, does not consume input or output tokens, and incurs no
inference cost. All decisions (deletion authorization, state reconstruction,
rule application, step ordering) are derived from participant-facing data in
`dataset/` using hardcoded policy logic and evidence-grounded lookups.

| Metric | Value |
|---|---|
| Model providers used | None |
| Model names | None |
| Model-call count | 0 |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request | 0 |
| Estimated total cost | $0.00 |
| Estimated per-request cost | $0.00 |

## Full-Dataset Run Summary

The agent was run once over all 518 evaluation requests in
`dataset/deletion_requests.csv`. The run produced `output.csv` at the
repository root.

| Metric | Value |
|---|---|
| Total requests processed | 518 |
| Output rows written | 518 |
| Output file | `output.csv` |
| Output file size | 639,617 bytes |
| COMPLETE cases | 99 (19.1%) |
| INCOMPLETE cases | 369 (71.2%) |
| NOT_APPLICABLE cases | 50 (9.7%) |
| Total steps generated | 3,218 |
| Average steps per case | 6.2 |
| Cases with empty steps_json | 42 (all COMPLETE with no remaining evidence at `audit_at`) |

### Determinism

The run is fully deterministic:
- Cases are processed in ascending `case_id` order.
- Evidence records are resolved by latest `captured_at` at or before `audit_at`.
- JSON serialization uses compact separators `(",", ":")` with stable key order.
- No randomness, no model sampling, no external API calls.

Re-running `python main.py` from the `code/` directory produces byte-identical
`output.csv`.

## Reproduction

```bash
cd hackerrank-orchestrate-october26/code
python main.py
```

This reads all input files from `dataset/` (relative to the code directory's
parent) and writes `output.csv` to the repository root.
