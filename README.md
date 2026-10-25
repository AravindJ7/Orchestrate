# Ghost Data Still Exists — Deletion-Audit Agent

HackerRank Orchestrate (October 2026) challenge submission.

An AI-powered deletion-audit agent that processes account-deletion requests,
reconstructs the true state of user data at each case's `audit_at` time from
participant-facing evidence, applies deletion policy, and produces a signed
audit verdict (`output.csv`).

## Challenge

For every request in `dataset/deletion_requests.csv` (518 cases), the agent:
1. Determines whether the request text actually authorizes deletion
   (`NOT_APPLICABLE` if not).
2. Resolves the request email to user(s), including historical identifiers
   recovered from the audit log.
3. Reconstructs the true state of every relevant record at `audit_at`,
   ignoring workflow `reported_status` claims.
4. Applies `dataset/deletion_rules.csv` to each record.
5. Detects active data flows and restore risks.
6. Generates ordered steps: FINDING → REMEDIATION → VERIFICATION.

## Repository Layout

```
Orchestrate/
├── log.txt                          # Agent session log (AGENTS.md requirement)
├── output.csv                       # Final submission: 518 rows, case_id,deletion_status,steps_json
├── README.md                        # This file
├── .env                             # API keys (not committed, not used by core logic)
├── .env.example                     # Template for NVIDIA NIM configuration
├── .gitignore
├── interactive_chat.py
├── prompts/
├── agent/                           # Optional NVIDIA NIM agent layer (tool-calling orchestrator)
│   ├── nvidia_client.py             # OpenAI-compatible client for integrate.api.nvidia.com
│   ├── model_discovery.py           # Lists models available to your key
│   ├── tools.py                     # Wraps the deterministic engine as LLM tools (lazy-loaded)
│   ├── prompts.py                   # System prompt (strict schema + workflow)
│   ├── orchestrator.py              # Tool-calling loop + output validation
│   └── test_agent.py                # CLI harness: python -m agent.test_agent case_05
├── ui/                              # Flask web UI (Full Dataset / Single Case / Upload / Agent Mode)
│   ├── app.py
│   ├── requirements.txt
│   ├── smoke_test.py                # Render + agent route smoke test (no server needed)
│   └── templates/
└── hackerrank-orchestrate-october26/
    ├── AGENTS.md                    # Challenge single source of truth
    ├── problem_statement.md
    ├── code/
    │   ├── main.py                  # Entry point
    │   ├── data_index.py            # Layer 1: data index
    │   ├── evidence_parser.py       # Layer 2: evidence parsing
    │   ├── audit_engine.py          # Layer 3: audit engine
    │   └── evaluation/
    │       ├── evaluation_report.md
    │       ├── usage_report.md
    │       └── main.py
    └── dataset/
        ├── deletion_requests.csv    # 518 evaluation requests
        ├── sample_deletion_requests.csv  # 32 public samples
        ├── sample_output.csv        # Expected outputs for samples
        ├── user_identities.csv
        ├── user_audit_log.csv
        ├── deletion_rules.csv
        ├── system_activity.csv
        ├── evidence_files.csv
        └── evidence/                # SQL, JSON, TXT, LOG, PDF, PNG artifacts
```

## Setup

No virtual environment or third-party packages are required for the core
agent. Python 3.8+ with the standard library is sufficient.

Optional PDF/image evidence parsing uses `pdfplumber`, `PyPDF2`, and
`Pillow` (install with `pip install pdfplumber PyPDF2 Pillow` if you want
full evidence coverage), but the agent runs correctly without them.

## Running

From the repository root:

```bash
cd hackerrank-orchestrate-october26/code
python main.py
```

This reads all input files from `dataset/` and writes `output.csv` to the
repository root (`Orchestrate/output.csv`).

Expected output:
- Exactly 518 data rows, one per `case_id` in `deletion_requests.csv`.
- Rows sorted by `case_id`.
- `deletion_status` ∈ {COMPLETE, INCOMPLETE, NOT_APPLICABLE}.
- `steps_json` is a valid JSON array with consecutive step numbering.

## Output Format

`output.csv` columns: `case_id,deletion_status,steps_json`

Each step object contains:
`step_no, phase, system_id, resource_id, operation, state, evidence_id`

- Findings cite participant-visible `evidence_id`.
- Remediation and post-remediation verification steps use `evidence_id=""`.
- COMPLETE cases include only evidence-backed verification steps.

## Web UI

A Flask UI wraps the same code (`ui/app.py`):

```bash
cd ui
pip install -r requirements.txt
python app.py          # http://localhost:5000
```

| Page | What it does |
|---|---|
| Full Dataset | Runs `DeletionAuditAgent.run()`, writes `output.csv`, offers download |
| Single Case | Runs `AuditEngine.analyze_case()` for form input, shows table + JSON |
| Upload & Analyze | Same analysis, and saves uploaded evidence files |
| Agent Mode | NVIDIA NIM tool-calling agent (see below) |

Smoke test without starting the server:

```bash
cd ui
python smoke_test.py
```

## Agent Mode (NVIDIA NIM)

Agent Mode adds an **LLM task manager** on top of the deterministic engine. The
model decides *which tool to call and when*; the audit logic remains the
rule-based engine, so the verdict is never hallucinated.

### Tools exposed to the model (`agent/tools.py`)

| Tool | Backing code |
|---|---|
| `resolve_identity(email)` | `DataIndex.resolve_user_ids()` (current + historical identities) |
| `search_evidence(user_ids, audit_at)` | `EvidenceParser.records_for_user_ids()` |
| `get_deletion_rules(data_category)` | `DataIndex.get_rules_for_category()` |
| `analyze_case(case)` | `AuditEngine.analyze_case()` |

The orchestrator (`agent/orchestrator.py`) runs a bounded tool-calling loop,
validates the returned steps against the required schema, and reports both the
agent answer and the **authoritative** engine result side by side in the UI.

### Setup

```bash
cp .env.example .env        # then paste your NVIDIA key
pip install openai httpx python-dotenv rich
python -m agent.model_discovery     # list models available to your key
```

Set the primary model (must support tool calling) in `.env`:

```env
NVIDIA_API_KEY=nvapi-...
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=openai/gpt-oss-20b
NVIDIA_FALLBACK_MODELS=nvidia/nemotron-3.5-lightning-30b-a3b,nvidia/nemotron-3-super-120b-a12b
```

### Test from the CLI

```bash
python -m agent.test_agent case_05
```

Expected output: the tool calls (`resolve_identity` → `search_evidence` →
`get_deletion_rules` → `analyze_case`), the agent's JSON answer, the
deterministic ground truth, and a comparison showing `steps_match: true`.

Agent Mode is optional: `python main.py` and `output.csv` are unaffected and
never call the network.

## Evaluation Reports

Required evaluation artifacts are in `code/evaluation/`:

| File | Contents |
|---|---|
| `evaluation_report.md` | Case-level, step-level, field-level, and sequence accuracy; 3 custom evaluation cases; failures found and improvements made |
| `usage_report.md` | Model usage (none — deterministic rule-based), token counts, cost, run summary |

## Submission Artifacts

Per the challenge contract (§6.9), the submission contains:

| Item | Location |
|---|---|
| `code.zip` | Runnable solution + `evaluation/` reports + `README.md` |
| `output.csv` | Repository root, one row per evaluation request |
| `log.txt` | Repository root, agent session log |
| AI interview | Explains architecture, investigation approach, remediation planning, evaluation |

`code.zip` must NOT include `dataset/`, virtual environments, dependency
caches, nested archives, API keys, credentials, real personal data, or
sensitive configuration.

## Challenge Link

https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

## Notes

- The challenge end time is not configured in this repository.
- All processing uses only participant-facing files under `dataset/`.
- No organizer-only files, hardcoded golden labels, or case-family shortcuts
  are used.
- Secrets (if any) are read from environment variables only; the `.env` file
  is uncommitted.
