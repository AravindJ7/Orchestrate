"""Smoke test for the Flask UI (no server needed): verifies pages render and agent route works."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import app

client = app.test_client()

for path in ["/", "/full_dataset", "/single_case", "/upload_analyze", "/agent_mode"]:
    resp = client.get(path)
    print(f"GET {path}: {resp.status_code} ({len(resp.data)} bytes)")
    assert resp.status_code == 200, path

payload = {
    "case_id": "case_05",
    "email": "leila.haddad@yahoo.com",
    "audit_at": "2026-09-08T13:40:00Z",
    "requested_at": "2026-09-08T13:40:00Z",
    "reported_status": "PARTIALLY_COMPLETE",
    "reported_at": "",
    "request_text": "I deleted this account and received a confirmation, but the same email address is still recognized. Please remove anything left.",
}
resp = client.post("/agent_analyze", data=payload, follow_redirects=True)
print(f"POST /agent_analyze: {resp.status_code} ({len(resp.data)} bytes)")
html = resp.data.decode("utf-8", "ignore")
for needle in ["Authoritative Result", "Agent Trace", "steps_json"]:
    print(f"  contains {needle!r}:", needle in html)
