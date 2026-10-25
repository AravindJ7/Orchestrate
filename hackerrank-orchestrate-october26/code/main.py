#!/usr/bin/env python3
"""Entry point for the Ghost Data Still Exists deletion-audit agent.

Runs the deterministic rule-based engine over all 518 requests in
``dataset/deletion_requests.csv`` and writes ``output.csv`` at the
repository root:

    case_id,deletion_status,steps_json

Usage (from the repository root):

    cd hackerrank-orchestrate-october26/code
    python main.py

No network access and no model calls are used here. The optional NVIDIA
Agent Mode lives in ``agent/`` and is only used by the web UI.
"""
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit_engine import DeletionAuditAgent  # noqa: E402


def main():
    agent = DeletionAuditAgent()
    rows = agent.run()

    statuses = {}
    for row in rows:
        statuses[row["deletion_status"]] = statuses.get(row["deletion_status"], 0) + 1

    summary = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
    print(f"Summary: {len(rows)} rows ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
