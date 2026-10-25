import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "hackerrank-orchestrate-october26" / "code"
DATASET = ROOT / "hackerrank-orchestrate-october26" / "dataset"

sys.path.insert(0, str(CODE))
sys.path.insert(0, str(ROOT))

from data_index import DataIndex
from evidence_parser import EvidenceParser
from audit_engine import AuditEngine

_lock = threading.Lock()
_state = {"index": None, "parser": None, "engine": None}


def _ensure_loaded():
    """Lazily build the deterministic engine singletons (once per process)."""
    if _state["engine"] is not None:
        return
    with _lock:
        if _state["engine"] is not None:
            return
        print("[AgentTools] Initializing index/parser/engine...")
        index = DataIndex(DATASET)
        parser = EvidenceParser(index)
        parser.parse_all()
        engine = AuditEngine(index, parser)
        _state["index"] = index
        _state["parser"] = parser
        _state["engine"] = engine
        print("[AgentTools] Ready.")


def get_index():
    _ensure_loaded()
    return _state["index"]


def get_parser():
    _ensure_loaded()
    return _state["parser"]


def get_engine():
    _ensure_loaded()
    return _state["engine"]


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "resolve_identity",
            "description": "Resolve a request email to user_ids using current identities and user_audit_log (includes historical emails).",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Email from deletion request"}
                },
                "required": ["email"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_evidence",
            "description": "Search parsed evidence for given user_ids at or before audit_at cutoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_ids": {"type": "array", "items": {"type": "string"}},
                    "audit_at": {"type": "string", "description": "ISO8601 cutoff e.g. 2026-07-19T13:00:00Z"}
                },
                "required": ["user_ids", "audit_at"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_deletion_rules",
            "description": "Get deletion rules for a data category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data_category": {"type": "string"}
                },
                "required": ["data_category"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_case",
            "description": "Run full deterministic audit for a deletion request and return deletion_status + steps list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "case": {
                        "type": "object",
                        "properties": {
                            "case_id": {"type": "string"},
                            "email": {"type": "string"},
                            "request_text": {"type": "string"},
                            "audit_at": {"type": "string"},
                            "requested_at": {"type": "string"},
                            "reported_status": {"type": "string"},
                            "reported_at": {"type": "string"}
                        },
                        "required": ["case_id", "email", "request_text", "audit_at"]
                    }
                },
                "required": ["case"]
            }
        }
    }
]


def run_tool(name: str, args: dict):
    try:
        if name == "resolve_identity":
            email = args.get("email", "")
            uids = get_index().resolve_user_ids(email)
            return {"email": email, "user_ids": sorted(list(uids))}

        elif name == "search_evidence":
            user_ids = set(args.get("user_ids", []))
            audit_at = args.get("audit_at", "")
            recs = get_parser().records_for_user_ids(user_ids, audit_at=audit_at)
            out = []
            for r in recs[:40]:
                out.append({
                    "file_id": r["file_id"],
                    "system_id": r["system_id"],
                    "resource_id": r["resource_id"],
                    "data_category": r["data_category"],
                    "state": r["state"],
                    "captured_at": r["captured_at"]
                })
            return {"count": len(recs), "records": out}

        elif name == "get_deletion_rules":
            cat = args.get("data_category", "")
            rules = get_index().get_rules_for_category(cat)
            return {"data_category": cat, "rules": rules}

        elif name == "analyze_case":
            case = args.get("case", {})
            status, steps = get_engine().analyze_case(case)
            return {"deletion_status": status, "steps": steps}

        else:
            return {"error": f"Unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}
