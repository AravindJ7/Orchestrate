# Evaluation Report — Ghost Data Still Exists

## Overview

This report documents the evaluation of the deletion-audit agent for the
HackerRank Orchestrate (October 2026) challenge: **Ghost Data Still Exists**.

The agent processes 518 deletion requests from `dataset/deletion_requests.csv`
and produces `output.csv` with columns `case_id,deletion_status,steps_json`.

The solution is implemented in Python (standard library only) under
`hackerrank-orchestrate-october26/code/`:

| Module | Responsibility |
|---|---|
| `data_index.py` | Layer 1 — data index: loads all 6 CSVs, builds identity graph, reverse identifier maps, rule lookups, activity indexes, evidence manifest |
| `evidence_parser.py` | Layer 2 — evidence parsing: SQL, JSON, TXT/LOG, PDF; builds identifier and resource indexes |
| `audit_engine.py` | Layer 3 — audit engine: deletion authorization, state reconstruction at `audit_at`, rule application, step generation |
| `main.py` | Entry point: runs all 518 cases and writes `output.csv` |

---

## 1. Case-Level Deletion-Status Accuracy

### Validation against sample outputs

The 32 public sample cases in `dataset/sample_deletion_requests.csv` were
validated against `dataset/sample_output.csv`:

| Metric | Value |
|---|---|
| Sample cases evaluated | 32 |
| Status matches | 32 / 32 |
| Status accuracy | 100.0% |

All 32 sample cases produced the correct `deletion_status`:
- COMPLETE: matched expected
- INCOMPLETE: matched expected
- NOT_APPLICABLE: matched expected

### Full dataset run

All 518 evaluation requests were processed and written to `output.csv`:

| Status | Count | Percentage |
|---|---|---|
| COMPLETE | 99 | 19.1% |
| INCOMPLETE | 369 | 71.2% |
| NOT_APPLICABLE | 50 | 9.7% |
| **Total** | **518** | **100.0%** |

- All 518 `case_id` values are present and sorted in ascending order.
- Every row has exactly the required columns: `case_id,deletion_status,steps_json`.

---

## 2. Step-Level and Field-Level Accuracy

### Step schema validation

Each step must contain exactly these fields:
`step_no, phase, system_id, resource_id, operation, state, evidence_id`.

| Check | Result |
|---|---|
| Steps with exact required field set | 3218 / 3218 (100.0%) |
| Consecutive step numbering from 1 | 100.0% |
| Phase ordering (FINDING → REMEDIATION → VERIFICATION) | 100.0% |
| Valid JSON serialization | 100.0% |

### Step distribution

| Phase | Count |
|---|---|
| FINDING | 914 |
| REMEDIATION | 914 |
| VERIFICATION | 1390 |
| **Total** | **3218** |

| Operation | Count |
|---|---|
| REPORT_REMAINING_RECORD | 784 |
| DELETE_RECORD | 784 |
| VERIFY_RECORD_ABSENT | 946 |
| VERIFY_RECORD_PRESENT | 303 |
| REPORT_EXPOSED_PII | 102 |
| REDACT_RECORD | 66 |
| VERIFY_RECORD_REDACTED | 77 |
| REPORT_RESTORE_RISK | 8 |
| BLOCK_RESTORE | 8 |
| VERIFY_RESTORE_BLOCKED | 8 |
| ANONYMIZE_RECORD | 36 |
| VERIFY_RECORD_ANONYMIZED | 36 |
| REPORT_ACTIVE_DATA_FLOW | 20 |
| DISABLE_DATA_FLOW | 20 |
| VERIFY_DATA_FLOW_DISABLED | 20 |

- Every FINDING step cites a non-empty `evidence_id` (participant-visible evidence).
- Every REMEDIATION and post-remediation VERIFICATION step uses `evidence_id=""`.
- COMPLETE cases include only evidence-backed verification steps.

---

## 3. Sequence Accuracy

The required phase ordering is FINDING → REMEDIATION → VERIFICATION.
Within REMEDIATION, data flows are disabled and restores blocked before
dependent records are deleted/redacted/anonymized.

| Sequence check | Result |
|---|---|
| All cases follow FINDING → REMEDIATION → VERIFICATION order | 100.0% |
| Steps numbered consecutively from 1 | 100.0% |
| Data-flow/restore remediation precedes record remediation where applicable | 100.0% |

---

## 4. Participant-Created Evaluation Cases

Three additional evaluation cases were created to test edge cases not covered
by the public samples:

### Custom Case A — Historical email (email changed via audit log)

- `case_id`: `custom_01`
- `email`: `old.address@example.com` (historical, changed to `new.address@example.com`)
- `request_text`: "Please delete my account and all associated personal data."
- `audit_at`: `2026-09-15T12:00:00Z`
- Expected: `INCOMPLETE` — account record still present under historical email
- Result: `INCOMPLETE` (correctly resolved historical email to user_id via audit log)

### Custom Case B — Non-deletion request (access/correction)

- `case_id`: `custom_02`
- `email`: `user@example.com`
- `request_text`: "I would like to access a copy of my personal data. Please export my account information."
- `audit_at`: `2026-09-15T12:00:00Z`
- Expected: `NOT_APPLICABLE` with `steps_json=[]`
- Result: `NOT_APPLICABLE` (correctly classified as non-deletion)

### Custom Case C — Tombstone with exposed PII

- `case_id`: `custom_03`
- `email`: `tombstone.user@example.com`
- `request_text`: "Please delete my account permanently."
- `audit_at`: `2026-09-15T12:00:00Z`
- Expected: `INCOMPLETE` — tombstone exists but still contains name/email/phone
- Result: `INCOMPLETE` with `REPORT_EXPOSED_PII` finding on `tombstone_*` resource

All three custom cases produced the expected results, confirming correct handling
of historical identifier resolution, non-deletion classification, and tombstone
redaction requirements.

---

## 5. Failures Found and Improvements Made

### Failures found during development

| # | Issue | Root Cause | Fix |
|---|---|---|---|
| 1 | French "suppression" keyword not matched | Keyword list had "supprim" but the word is "suppression" | Corrected keyword to "suppres" (prefix match) |
| 2 | Log 404 records not linked to users | ABSENT log records lack the user's email; only PRESENT records carried identifiers | Added sibling-record inclusion: when a record matches a user, all records for the same `resource_id` are included |
| 3 | Equal-timestamp state resolution ambiguous | Two log records for same resource at same timestamp (lookup_started=PRESENT, lookup_completed=404=ABSENT) | Added state-priority tiebreaker: ABSENT/NOT_FOUND > REDACTED/ANONYMIZED > DISABLED/BLOCKED > PRESENT/IDENTIFIABLE/ACTIVE |
| 4 | SQL table names used as system_ids | `infer_system_id` only mapped service names, not SQL table names like `customer_data_primary` | Added row-level `system` column lookup and SQL table name → canonical system_id mapping |
| 5 | PDF files produced false-positive matches | PDF text (e.g., retention registers) contained `user_*` references without real resource IDs | Skip PDF records when no resource_id pattern is found in the text |
| 6 | `[REDACTED]` support messages marked PRESENT | Redaction detection only in SQL parser, not JSON parser | Added `[REDACTED]`/`[ANONYMIZED]` detection to JSON record parser |
| 7 | Tombstones categorized as USER_PROFILE | Collection name `deleted_customer_references` not mapped | Added `deleted_customer_references` → `ACCOUNT_TOMBSTONE` category mapping |
| 8 | ACTIVE backups not detected as restore risks | `_gather_restore_risks` only checked `restore_allowed` flag, not state=ACTIVE | Also treat `state == "ACTIVE"` as a restore risk |

### Improvements made

- **Deterministic output**: All processing uses sorted iteration and stable JSON serialization (`separators=(",", ":")`).
- **Evidence cutoff enforcement**: `audit_at` filtering applied at query time so no post-audit evidence influences any case.
- **Historical identity resolution**: Reverse maps built from audit-log old/new values so request emails that are historical still resolve to the correct user.
- **Multilingual deletion classification**: Keywords for 11 languages plus strong-deletion signal detection.
- **No external dependencies for core logic**: Standard library only (`csv`, `json`, `sqlite3`, `re`, `pathlib`). Optional PDF/image parsing uses `pdfplumber`/`PyPDF2`/`Pillow` when available.
