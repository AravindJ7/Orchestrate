"""CLI test harness for the NVIDIA agent mode.

Usage (from repo root):
    python -m agent.test_agent            # run case_05
    python -m agent.test_agent case_12    # run any sample case_id
"""
import csv
import json
import sys
from pathlib import Path

from agent.orchestrator import AgentOrchestrator

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "hackerrank-orchestrate-october26" / "dataset"


def load_case(case_id="case_05"):
    samples = DATASET / "sample_deletion_requests.csv"
    with open(samples, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["case_id"] == case_id:
                return row
    raise SystemExit(f"{case_id} not found in {samples}")


def main():
    case_id = sys.argv[1] if len(sys.argv) > 1 else "case_05"
    case = load_case(case_id)
    print(f"Testing agent on {case_id} ({case['email']})")
    result = AgentOrchestrator().analyze(case)

    print("\n=== MODEL ===")
    print(result.get("model"))
    if result.get("error"):
        print("\n=== ERROR ===")
        print(result["error"])

    print("\n=== TOOL CALLS ===")
    for t in result.get("trace", []):
        if t["type"] == "tool_call":
            print(f"- {t['tool']}: {json.dumps(t['args'])[:200]}")

    print("\n=== AGENT ANSWER ===")
    print(result.get("answer") or "(none)")

    det = result.get("deterministic") or result.get("deterministic_fallback")
    print("\n=== DETERMINISTIC GROUND TRUTH ===")
    if det:
        print(json.dumps(det, indent=2)[:2000])
    else:
        print("(unavailable)")

    parsed = result.get("parsed")
    print("\n=== PARSED AGENT OUTPUT ===")
    print(json.dumps(parsed, indent=2)[:1500] if parsed else "(none)")
    if parsed and det:
        print("\n=== MATCH ===")
        print("comparison:", json.dumps(result.get("comparison"), indent=2))
        print("status match:", parsed.get("deletion_status") == det.get("deletion_status"))
        print("step count:", len(parsed.get("steps") or []), "vs", len(det.get("steps") or []))


if __name__ == "__main__":
    main()
