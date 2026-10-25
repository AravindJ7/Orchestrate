#!/usr/bin/env python3
"""
Evidence parsing layer for the Ghost Data Still Exists deletion-audit agent.

Task 2 - Parse evidence contents and build searchable indexes.

This module inspects every evidence file (SQL, JSON, TXT, LOG, PDF, PNG) listed in
``evidence_files.csv``, extracts the structured facts each file contains, and builds
indexes so downstream phases can:

  * find which evidence records mention a given user identifier (current or historical),
  * reconstruct the state of each resource at a case's ``audit_at`` time,
  * classify each record against ``deletion_rules.csv`` (DELETE / REDACT / ANONYMIZE /
    RETAIN / ACCOUNT_TOMBSTONE),
  * detect active data flows and restore risks.

Only text-decodable formats are content-parsed (SQL, JSON, TXT, LOG). PDF and PNG
files are recorded with their manifest metadata; their content is not readable
without optional libraries (pdfplumber / PyPDF2 / PIL / pytesseract), so they are
represented as opaque evidence items keyed by file_id.

Parsed evidence record shape (one per discoverable record / event):

{
  "file_id": "evidence_01",
  "resource_id": "account_005",
  "system_id": "account_service",
  "data_category": "USER_PROFILE",          # inferred from table / collection / record_type
  "state": "PRESENT",                        # PRESENT | ABSENT | NOT_FOUND | FOUND |
                                              ACTIVE | DISABLED | BLOCKED | REDACTED |
                                              ANONYMIZED | IDENTIFIABLE
  "captured_at": "2026-09-15T11:59:39Z",
  "identifiers": {                           # every direct identifier found in the record
     "emails": ["a@b.com"],
     "phones": [],
     "names": [],
     "email_hashes": [],
     "user_ids": ["user_05"]
  },
  "raw": {...}                               # original parsed object / row
}

Indexes maintained:
  * self.evidence_records[file_id] -> list of parsed records
  * self.identifier_index[normalized_id] -> list of (file_id, record_index)
  * self.resource_index[resource_id] -> list of (file_id, record_index)
  * self.unparsed_files[file_id] -> manifest row (PDF / PNG / unreadable)
"""

import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Optional PDF parsing
try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    pdfplumber = None
    _HAS_PDFPLUMBER = False

try:
    import PyPDF2
    _HAS_PYPDF2 = True
except ImportError:
    PyPDF2 = None
    _HAS_PYPDF2 = False

# Optional image parsing (metadata only; OCR requires tesseract binary)
try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    Image = None
    _HAS_PIL = False

from data_index import DataIndex, parse_ts, normalize_email, normalize_value


# ---------------------------------------------------------------------------
# Category inference helpers
# ---------------------------------------------------------------------------

TABLE_TO_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "support_records": "SUPPORT_MESSAGE",
    "support_attachments": "SUPPORT_ATTACHMENT",
    "customer_exports": "USER_EXPORT",
    "connector_subscriptions": "MARKETING_PROFILE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_records": "SECURITY_AUDIT_LOG",
}

COLLECTION_TO_CATEGORY = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_records": "SUPPORT_MESSAGE",
    "support_attachments": "SUPPORT_ATTACHMENT",
    "customer_exports": "USER_EXPORT",
    "customer_export_jobs": "USER_EXPORT",
    "connector_configurations": "MARKETING_PROFILE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "tombstones": "ACCOUNT_TOMBSTONE",
    "deleted_accounts": "ACCOUNT_TOMBSTONE",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
}

# System name -> canonical system_id used in output steps
SYSTEM_ID_MAP = {
    "account_service": "account_service",
    "search_service": "search_service",
    "analytics_platform": "analytics_platform",
    "analytics_identity_store": "analytics_platform",
    "support_platform": "support_platform",
    "export_service": "export_service",
    "integration_service": "integration_service",
    "backup_service": "backup_service",
    "security_service": "security_service",
    "identity_service": "identity_service",
    "notification_service": "notification_service",
    "email_delivery_service": "notification_service",
    "object_store": "object_store",
    "cache_service": "cache_service",
    # SQL table names -> canonical system_id
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "customer_analytics_reports": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def infer_system_id(data_store_or_system):
    """Map a raw data_store / system name to a canonical system_id."""
    if not data_store_or_system:
        return "unknown"
    key = str(data_store_or_system).strip()
    return SYSTEM_ID_MAP.get(key, key)


def infer_category(collection_or_table_or_record_type, record_type_hint=None):
    """Infer the deletion-rule data_category from a table/collection/record_type name."""
    key = None
    for candidate in (collection_or_table_or_record_type, record_type_hint):
        if not candidate:
            continue
        c = str(candidate).strip()
        if c in TABLE_TO_CATEGORY:
            return TABLE_TO_CATEGORY[c]
        if c in COLLECTION_TO_CATEGORY:
            return COLLECTION_TO_CATEGORY[c]
        key = c
    # Fallback: substring matching
    if key:
        kl = key.lower()
        if "tombstone" in kl or "deleted_account" in kl:
            return "ACCOUNT_TOMBSTONE"
        if "search" in kl or "index" in kl:
            return "SEARCH_INDEX_ENTRY"
        if "analytic" in kl:
            return "ANALYTICS_PROFILE"
        if "support" in kl:
            return "SUPPORT_MESSAGE"
        if "export" in kl:
            return "USER_EXPORT"
        if "connector" in kl or "marketing" in kl:
            return "MARKETING_PROFILE"
        if "backup" in kl or "restore" in kl or "archive" in kl:
            return "BACKUP_ARCHIVE"
        if "security" in kl or "audit" in kl:
            return "SECURITY_AUDIT_LOG"
        if "profile" in kl or "account" in kl or "customer" in kl:
            return "USER_PROFILE"
    return "USER_PROFILE"


def map_result_to_state(result, data_category):
    """Map a raw result/status string to a canonical step state."""
    if result is None:
        return "PRESENT"
    r = str(result).strip().lower()
    if r in ("not_found", "404", "absent", "deleted", "anonymized", "redacted",
             "disabled", "blocked", "retained"):
        return r.upper()
    if r in ("found", "present", "active", "enabled", "identifiable", "eligible"):
        return {
            "found": "PRESENT",
            "present": "PRESENT",
            "active": "ACTIVE",
            "enabled": "ACTIVE",
            "identifiable": "IDENTIFIABLE",
            "eligible": "ACTIVE",
        }.get(r, "PRESENT")
    if r == "anonymized":
        return "ANONYMIZED"
    if r == "redacted":
        return "REDACTED"
    return "PRESENT"


# ---------------------------------------------------------------------------
# Identifier extraction from arbitrary text / dict
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Match phone numbers but NOT ISO-8601 timestamps/dates like 2026-10-01T12:00:00Z
PHONE_RE = re.compile(r"(?<!\d)(?<!\dT)(?<!\d-)\+?\d[\d\s()\-]{6,}\d(?!\d)")
USER_ID_RE = re.compile(r"\buser_\d+\b")
RESOURCE_ID_RE = re.compile(r"\b(?:account|search|analytics|support|export|backup|audit_record|data_flow|tombstone)_\d+\b")
DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def extract_identifiers_from_text(text):
    """Pull emails, phones, user_ids out of free text."""
    if not text:
        return {"emails": [], "phones": [], "names": [], "email_hashes": [], "user_ids": []}
    emails = [normalize_email(m) for m in EMAIL_RE.findall(text)]
    emails = [e for e in emails if e]
    phones = []
    for m in PHONE_RE.findall(text):
        n = normalize_value(m)
        # Reject ISO-8601 timestamps/dates masquerading as phone numbers
        if not n or DATE_ONLY_RE.fullmatch(n):
            continue
        phones.append(n)
    user_ids = list(dict.fromkeys(USER_ID_RE.findall(text)))
    return {
        "emails": emails,
        "phones": phones,
        "names": [],
        "email_hashes": [],
        "user_ids": user_ids,
    }


def extract_identifiers_from_obj(obj):
    """Recursively extract identifiers from a JSON dict/list/scalar."""
    emails, phones, names, hashes, user_ids = set(), set(), set(), set(), set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if isinstance(v, str):
                    if any(term in kl for term in ("email", "mail")):
                        n = normalize_email(v)
                        if n:
                            emails.add(n)
                    elif any(term in kl for term in ("phone", "msisdn")):
                        n = normalize_value(v)
                        if n:
                            phones.add(n)
                    elif any(term in kl for term in ("name", "full_name", "display_name", "requester_name", "subject_reference", "customer_reference")):
                        n = normalize_value(v)
                        if n:
                            names.add(n)
                    elif "hash" in kl:
                        n = normalize_value(v)
                        if n:
                            hashes.add(n)
                    elif kl in ("user_id", "customer_reference", "subject_reference"):
                        n = normalize_value(v)
                        if n and (n.startswith("user_") or "@" not in n):
                            user_ids.add(n)
                    # Also scan the string value for embedded identifiers
                    found = extract_identifiers_from_text(v)
                    emails.update(found["emails"])
                    phones.update(found["phones"])
                    user_ids.update(found["user_ids"])
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            found = extract_identifiers_from_text(node)
            emails.update(found["emails"])
            phones.update(found["phones"])
            user_ids.update(found["user_ids"])

    walk(obj)
    return {
        "emails": sorted(emails),
        "phones": sorted(phones),
        "names": sorted(names),
        "email_hashes": sorted(hashes),
        "user_ids": sorted(user_ids),
    }


# ---------------------------------------------------------------------------
# Evidence parser
# ---------------------------------------------------------------------------

class EvidenceParser:
    def __init__(self, data_index):
        self.index = data_index
        self.dataset_dir = data_index.dataset_dir

        # Parsed records: file_id -> list of evidence-record dicts
        self.evidence_records = {}
        # file_id -> manifest row for files we could not content-parse (PDF/PNG/etc.)
        self.unparsed_files = {}
        # file_id -> parsed success flag + error message
        self.parse_status = {}

        # Indexes
        self.identifier_index = {}   # normalized identifier -> [(file_id, record_idx), ...]
        self.resource_index = {}     # resource_id -> [(file_id, record_idx), ...]

    # ------------------------------------------------------------------ main
    def parse_all(self, audit_at=None):
        """Parse every evidence file.

        All evidence is parsed and stored; audit_at filtering is applied at
        query time (records_for_user_ids, records_for_resource, etc.) so the
        parser can be reused across cases with different audit_at cutoffs.

        Returns a dict of file_id -> parse status summary.
        """
        # Reset indexes for a clean parse
        self.evidence_records = {}
        self.unparsed_files = {}
        self.parse_status = {}
        self.identifier_index = {}
        self.resource_index = {}

        for ef in self.index.evidence_files:
            fid = ef.get("file_id")
            self.parse_file(fid)
        return self.parse_status

    def parse_file(self, file_id):
        """Parse a single evidence file by extension."""
        ef = self.index.evidence_by_id.get(file_id)
        if not ef:
            self.parse_status[file_id] = {"error": "not_in_manifest"}
            return {"error": "not_in_manifest"}

        full_path = Path(ef["_full_path"])
        if not ef.get("_exists") or not full_path.exists():
            self.parse_status[file_id] = {"error": "file_missing", "path": str(full_path)}
            self.unparsed_files[file_id] = ef
            return {"error": "file_missing"}

        ext = full_path.suffix.lower()
        try:
            if ext == ".sql":
                records = self._parse_sql(file_id, full_path)
            elif ext == ".json":
                records = self._parse_json(file_id, full_path)
            elif ext in (".txt", ".log"):
                records = self._parse_text(file_id, full_path)
            elif ext == ".pdf":
                records = self._parse_pdf(file_id, full_path)
            elif ext in (".png", ".jpg", ".jpeg"):
                # Image evidence: extract metadata only (OCR needs tesseract binary)
                records = self._parse_image(file_id, full_path)
            else:
                records = []
                self.unparsed_files[file_id] = ef

            self.evidence_records[file_id] = records
            self._index_records(file_id, records)
            self.parse_status[file_id] = {
                "parsed": True,
                "record_count": len(records),
                "format": ext,
            }
            return self.parse_status[file_id]
        except Exception as exc:  # pragma: no cover - defensive
            self.parse_status[file_id] = {"error": str(exc), "format": ext}
            self.unparsed_files[file_id] = ef
            return self.parse_status[file_id]

    # ----------------------------------------------------------- SQL parsing
    def _parse_sql(self, file_id, path):
        """Execute a SQLite snapshot export in an in-memory DB and extract all rows."""
        records = []
        con = sqlite3.connect(":memory:")
        try:
            with open(path, encoding="utf-8-sig") as f:
                script = f.read()
            con.executescript(script)

            # Discover tables
            cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]

            captured_at = self._captured_at_from_sql(script)

            for table in tables:
                data_category = infer_category(table)
                # Get column names via PRAGMA (cursor.description is invalid after fetchall)
                try:
                    pragma = con.execute(f"PRAGMA table_info({table})").fetchall()
                except sqlite3.Error:
                    continue
                col_names = [r[1] for r in pragma]
                if not col_names:
                    continue
                try:
                    rows = con.execute(f"SELECT * FROM {table}").fetchall()
                except sqlite3.Error:
                    continue

                for row in rows:
                    row_dict = dict(zip(col_names, row))
                    record = self._build_record_from_row(
                        file_id=file_id,
                        table=table,
                        data_category=data_category,
                        captured_at=captured_at,
                        row_dict=row_dict,
                    )
                    if record:
                        records.append(record)
        finally:
            con.close()
        return records

    def _captured_at_from_sql(self, script):
        m = re.search(r"--\s*snapshot_completed_at\s*=\s*([^\n]+)", script)
        if m:
            return m.group(1).strip()
        m = re.search(r"captured_at\s*=\s*'([^']+)'", script)
        if m:
            return m.group(1).strip()
        return None

    def _build_record_from_row(self, file_id, table, data_category, captured_at, row_dict):
        # Find resource_id column
        rid = None
        for key in ("resource_id", "id"):
            if key in row_dict and row_dict[key] is not None:
                rid = str(row_dict[key])
                break
        if not rid:
            return None

        system_id = infer_system_id(table)
        # If the row has an explicit system column, prefer it over the table name
        for sys_col in ("system", "origin_system", "source_system", "data_store"):
            if sys_col in row_dict and row_dict[sys_col]:
                system_id = infer_system_id(row_dict[sys_col])
                break
        state = map_result_to_state(row_dict.get("account_status") or row_dict.get("status") or row_dict.get("result"), data_category)
        # Special handling: account_status DELETION_PENDING / ACTIVE means PRESENT
        acct_status = (row_dict.get("account_status") or "").strip().lower()
        if acct_status in ("active", "deletion_pending"):
            state = "PRESENT"
        # Detect redaction in text/message fields
        for k, v in row_dict.items():
            if isinstance(v, str) and v.strip() == "[REDACTED]":
                state = "REDACTED"
                break
        # Detect anonymization
        for k, v in row_dict.items():
            if isinstance(v, str) and v.strip() == "[ANONYMIZED]":
                state = "ANONYMIZED"
                break
        # For ACCOUNT_TOMBSTONE, if direct identifiers (email/phone) are still
        # present, the tombstone is exposed regardless of account_status.
        # Internal references (subject_reference, customer_reference) are not PII.
        if data_category == "ACCOUNT_TOMBSTONE":
            ids = extract_identifiers_from_obj(row_dict)
            if ids["emails"] or ids["phones"]:
                state = "PRESENT"

        identifiers = extract_identifiers_from_obj(row_dict)

        return {
            "file_id": file_id,
            "resource_id": rid,
            "system_id": system_id,
            "data_category": data_category,
            "state": state,
            "captured_at": captured_at,
            "identifiers": identifiers,
            "raw": row_dict,
            "table": table,
        }

    # ----------------------------------------------------------- JSON parsing
    def _parse_json(self, file_id, path):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)

        records = []
        captured_at = data.get("exported_at") or data.get("captured_at")

        # Schema A: snapshot bundle -> snapshots[] with data_store/collection/records
        if "snapshots" in data and isinstance(data["snapshots"], list):
            for snap in data["snapshots"]:
                snap_captured = snap.get("captured_at") or captured_at
                data_store = snap.get("data_store")
                collection = snap.get("collection")
                data_category = infer_category(collection)
                system_id = infer_system_id(data_store)
                for rec in snap.get("records", []):
                    records.append(self._json_record_to_evidence(
                        file_id, rec, data_category, system_id, snap_captured, "snapshot_bundle"))
            return records

        # Schema B: service_resource_export -> service_exports[service][profile_lookups|profiles]
        if "service_exports" in data and isinstance(data["service_exports"], dict):
            for service_name, service_block in data["service_exports"].items():
                system_id = infer_system_id(service_name)
                for list_key in ("profile_lookups", "profiles", "records"):
                    items = service_block.get(list_key)
                    if not isinstance(items, list):
                        continue
                    data_category = infer_category(list_key or service_name)
                    for rec in items:
                        records.append(self._json_record_to_evidence(
                            file_id, rec, data_category, system_id, captured_at, "service_export"))
            return records

        # Schema C: flat records[] (connector_configurations, security_audit_ledger,
        # customer_analytics_report, customer_export_manifest, customer_export_jobs)
        if "records" in data and isinstance(data["records"], list):
            system = data.get("system") or data.get("origin") or data.get("source")
            system_id = infer_system_id(system)
            data_category = infer_category(data.get("collection") or data.get("origin") or "")
            for rec in data["records"]:
                records.append(self._json_record_to_evidence(
                    file_id, rec, data_category, system_id, captured_at, "flat_records"))
            return records

        # Schema D: unknown JSON shape - extract top-level identifiers generically
        identifiers = extract_identifiers_from_obj(data)
        rid = str(data.get("resource_id") or data.get("id") or "")
        if rid:
            records.append({
                "file_id": file_id,
                "resource_id": rid,
                "system_id": infer_system_id(data.get("source")),
                "data_category": "USER_PROFILE",
                "state": "PRESENT",
                "captured_at": captured_at,
                "identifiers": identifiers,
                "raw": data,
                "table": "json_root",
            })
        return records

    def _json_record_to_evidence(self, file_id, rec, data_category, system_id, captured_at, schema):
        if not isinstance(rec, dict):
            return None
        rid = rec.get("resource_id") or rec.get("id")
        if not rid:
            return None
        rid = str(rid)

        result = rec.get("result") or rec.get("status_code") or rec.get("account_status")
        state = map_result_to_state(result, data_category)
        # HTTP status 200 with a profile present => PRESENT; 404 / null profile => ABSENT/NOT_FOUND
        if rec.get("profile") is None and rec.get("status_code") == 404:
            state = "ABSENT"
        if rec.get("status_code") == 200 and rec.get("profile") is not None:
            state = "PRESENT"

        # Detect redaction / anonymization in any text field
        for v in rec.values():
            if isinstance(v, str):
                if v.strip() == "[REDACTED]":
                    state = "REDACTED"
                elif v.strip() == "[ANONYMIZED]":
                    state = "ANONYMIZED"

        # For ACCOUNT_TOMBSTONE, if direct identifiers (email/phone) are still
        # present, the tombstone is exposed regardless of account_status.
        # Internal references (subject_reference, customer_reference) are not PII.
        if data_category == "ACCOUNT_TOMBSTONE":
            ids = extract_identifiers_from_obj(rec)
            if ids["emails"] or ids["phones"]:
                state = "PRESENT"

        identifiers = extract_identifiers_from_obj(rec)

        return {
            "file_id": file_id,
            "resource_id": rid,
            "system_id": system_id or infer_system_id(rec.get("system")),
            "data_category": data_category,
            "state": state,
            "captured_at": captured_at,
            "identifiers": identifiers,
            "raw": rec,
            "schema": schema,
        }

    # ----------------------------------------------------------- text/log parsing
    def _parse_text(self, file_id, path):
        """Parse TXT handover notes and LOG files line by line."""
        records = []
        captured_at = None
        # Try to infer a capture time from the first lines / filename
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return records

        # Handover-style structured lines:
        # - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.lindberg18@yahoo.com
        handover_re = re.compile(
            r"^[-*\s]*(?P<ts>\S+)\s*\|\s*(?P<system>[^\|]+?)\s*\|\s*(?P<rid>[^\|]+?)\s*\|\s*"
            r"(?P<category>[^\|]+?)\s*\|\s*(?P<status>[^\|]+?)\s*\|\s*(?P<ident>.*)$"
        )

        # Structured log lines: key=value pairs, may include resource_id=, event=, matched_value=,
        # correlation_key=, subject_key=, lookup_key=, subject_ref=
        log_kv_re = re.compile(r"(\w+)=\"(?P<q>[^\"]*?)\"|(\w+)=(?P<unq>\S+)")

        # Timestamp at start of log lines: 2026-09-26T08:57:37Z ...
        line_ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2}))")

        for line in lines:
            line = line.rstrip("\n")
            if not line.strip():
                continue

            # Extract per-line timestamp from log lines
            line_ts = None
            mts = line_ts_re.match(line)
            if mts:
                line_ts = mts.group(1)

            # Try handover pattern first
            m = handover_re.match(line)
            if m:
                captured_at = m.group("ts")
                system = m.group("system").strip()
                rid = m.group("rid").strip()
                category = m.group("category").strip()
                status = m.group("status").strip()
                ident_text = m.group("ident").strip()
                data_category = infer_category(category)
                state = map_result_to_state(status, data_category)
                identifiers = extract_identifiers_from_text(ident_text)
                # Also pull identifiers from the whole line
                line_ids = extract_identifiers_from_text(line)
                for k in identifiers:
                    identifiers[k] = sorted(set(identifiers[k]) | set(line_ids[k]))
                records.append({
                    "file_id": file_id,
                    "resource_id": rid,
                    "system_id": infer_system_id(system),
                    "data_category": data_category,
                    "state": state,
                    "captured_at": line_ts or captured_at,
                    "identifiers": identifiers,
                    "raw": {"line": line, "parsed": m.groupdict()},
                })
                continue

            # Generic structured log: pull resource_id and event, plus identifiers
            kv = {}
            for kmatch in log_kv_re.finditer(line):
                key = kmatch.group(1) or kmatch.group(3)
                val = kmatch.group("q") or kmatch.group("unq")
                if key and val is not None:
                    kv[key] = val

            rid = kv.get("resource_id") or kv.get("resourceid")
            if rid:
                event = kv.get("event", "")
                data_category = infer_category(event)
                system = kv.get("service", kv.get("source_system", ""))
                http_status = kv.get("http_status", "")
                rows = kv.get("rows", "")
                result = kv.get("result", "")
                matched = kv.get("matched_value", "")
                eligible = kv.get("eligible", "")
                enabled = kv.get("enabled", "")

                # Determine state from log status indicators
                state = "PRESENT"
                if http_status == "404" or rows == "0" or result in ("miss", "not_found"):
                    state = "ABSENT"
                elif result == "hit" or matched:
                    state = "PRESENT"
                elif "heartbeat" in line or enabled == "true":
                    state = "ACTIVE"
                elif eligible == "true":
                    state = "ACTIVE"
                elif matched and data_category in ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE"):
                    state = "IDENTIFIABLE"

                identifiers = extract_identifiers_from_text(line)
                # If matched_value present, add it as an identifier
                if matched:
                    found = extract_identifiers_from_text(matched)
                    identifiers["emails"] = sorted(set(identifiers["emails"]) | set(found["emails"]))
                    identifiers["phones"] = sorted(set(identifiers["phones"]) | set(found["phones"]))
                records.append({
                    "file_id": file_id,
                    "resource_id": rid,
                    "system_id": infer_system_id(system),
                    "data_category": data_category,
                    "state": state,
                    "captured_at": line_ts or captured_at,
                    "identifiers": identifiers,
                    "raw": {"line": line, "kv": kv},
                })
                continue

            # Fallback: any line containing identifiers -> record it as an evidence mention
            ids = extract_identifiers_from_text(line)
            if ids["emails"] or ids["phones"] or ids["user_ids"]:
                rid_match = RESOURCE_ID_RE.search(line)
                records.append({
                    "file_id": file_id,
                    "resource_id": rid_match.group(0) if rid_match else f"unknown_{file_id}",
                    "system_id": "unknown",
                    "data_category": "USER_PROFILE",
                    "state": "PRESENT",
                    "captured_at": line_ts or captured_at,
                    "identifiers": ids,
                    "raw": {"line": line},
                })

        return records

    # ----------------------------------------------------------- PDF parsing
    def _parse_pdf(self, file_id, path):
        """Extract text from a PDF and turn it into evidence records."""
        records = []
        captured_at = None
        # Try to get captured_at from manifest is handled by caller; here we
        # just extract text. The manifest captured_at is used at query time.
        full_text = ""
        try:
            if _HAS_PDFPLUMBER:
                with pdfplumber.open(path) as pdf:
                    for page in pdf.pages:
                        page_text = page.extract_text() or ""
                        full_text += page_text + "\n"
            elif _HAS_PYPDF2:
                reader = PyPDF2.PdfReader(str(path))
                for page in reader.pages:
                    full_text += (page.extract_text() or "") + "\n"
        except Exception:
            pass

        if full_text.strip():
            identifiers = extract_identifiers_from_text(full_text)
            # Try to find a resource_id in the text
            rid_match = RESOURCE_ID_RE.search(full_text)
            rid = rid_match.group(0) if rid_match else None
            if not rid:
                # No real resource found in a PDF - skip it. PDF documents like
                # retention registers don't contain per-resource evidence.
                return records
            # Try to find a system hint
            system = "unknown"
            for sys_name in SYSTEM_ID_MAP:
                if sys_name in full_text.lower():
                    system = SYSTEM_ID_MAP[sys_name]
                    break
            records.append({
                "file_id": file_id,
                "resource_id": rid,
                "system_id": system,
                "data_category": "USER_PROFILE",
                "state": "PRESENT",
                "captured_at": captured_at,
                "identifiers": identifiers,
                "raw": {"text": full_text[:2000]},
            })
        return records

    # ----------------------------------------------------------- Image parsing
    def _parse_image(self, file_id, path):
        """Extract basic metadata from an image (no OCR without tesseract)."""
        records = []
        try:
            if _HAS_PIL:
                with Image.open(path) as img:
                    # Record image dimensions and format as metadata
                    _ = img.size, img.format
        except Exception:
            pass
        # Opaque image - record as an evidence mention with no content
        identifiers = extract_identifiers_from_text(file_id)
        records.append({
            "file_id": file_id,
            "resource_id": f"unknown_{file_id}",
            "system_id": "unknown",
            "data_category": "USER_PROFILE",
            "state": "PRESENT",
            "captured_at": None,
            "identifiers": identifiers,
            "raw": {"note": "image content not OCR-readable"},
        })
        return records

    # ----------------------------------------------------------- indexing
    def _index_records(self, file_id, records):
        for idx, rec in enumerate(records):
            rid = rec.get("resource_id")
            if rid:
                self.resource_index.setdefault(rid, []).append((file_id, idx))
            for ident in rec.get("identifiers", {}).get("emails", []):
                self.identifier_index.setdefault(ident, []).append((file_id, idx))
            for ident in rec.get("identifiers", {}).get("phones", []):
                self.identifier_index.setdefault(ident, []).append((file_id, idx))
            for ident in rec.get("identifiers", {}).get("user_ids", []):
                self.identifier_index.setdefault(ident, []).append((file_id, idx))
            for ident in rec.get("identifiers", {}).get("email_hashes", []):
                self.identifier_index.setdefault(ident, []).append((file_id, idx))
            for ident in rec.get("identifiers", {}).get("names", []):
                self.identifier_index.setdefault(ident, []).append((file_id, idx))

    # ----------------------------------------------------------- queries
    def records_for_file(self, file_id):
        return list(self.evidence_records.get(file_id, []))

    def records_by_identifier(self, identifier):
        """All parsed evidence records mentioning a normalized identifier."""
        results = []
        for file_id, idx in self.identifier_index.get(identifier, []):
            results.append(self.evidence_records[file_id][idx])
        return results

    def records_for_user_ids(self, user_ids, audit_at=None):
        """All evidence records relevant to any of the given user_ids.

        Matches on user_id directly, or on any email/phone/name/email_hash known
        for those users via the DataIndex identity graph. Also includes sibling
        records for the same resource_id (a 404/lookup record may not contain the
        user's identifier, but belongs to the same resource as a matching record).
        """
        cutoff = parse_ts(audit_at) if audit_at else None
        all_identifiers = set(user_ids)
        for uid in user_ids:
            graph = self.index.get_identity_graph(uid)
            if graph:
                all_identifiers.update(graph["emails"])
                all_identifiers.update(graph["phones"])
                all_identifiers.update(graph["names"])
                all_identifiers.update(graph["email_hashes"])

        seen = set()
        results = []
        # First pass: match by identifier
        for ident in all_identifiers:
            n = normalize_email(ident) if "@" in str(ident) else normalize_value(ident)
            if not n:
                continue
            for rec in self.records_by_identifier(n):
                key = (rec["file_id"], rec["resource_id"])
                if key in seen:
                    continue
                if cutoff is not None:
                    cap = parse_ts(rec.get("captured_at", ""))
                    if cap is not None and cap > cutoff:
                        continue
                seen.add(key)
                results.append(rec)

        # Second pass: add sibling records for the same resource_id
        sibling_keys = {(r["file_id"], r["resource_id"]) for r in results}
        for fid, rid in sibling_keys:
            for rec in self.records_for_resource(rid, audit_at=audit_at):
                key = (rec["file_id"], rec["resource_id"], rec["state"])
                if key in seen:
                    continue
                seen.add(key)
                results.append(rec)
        return results

    def records_for_resource(self, resource_id, audit_at=None):
        """All parsed evidence records for a resource, optionally filtered by audit_at."""
        cutoff = parse_ts(audit_at) if audit_at else None
        results = []
        for file_id, idx in self.resource_index.get(resource_id, []):
            rec = self.evidence_records[file_id][idx]
            if cutoff is not None:
                cap = parse_ts(rec.get("captured_at", ""))
                if cap is not None and cap > cutoff:
                    continue
            results.append(rec)
        return results

    def summary(self):
        total_records = sum(len(v) for v in self.evidence_records.values())
        return {
            "files_parsed": len(self.evidence_records),
            "files_unparsed": len(self.unparsed_files),
            "total_records": total_records,
            "identifier_index_entries": len(self.identifier_index),
            "resource_index_entries": len(self.resource_index),
        }


def main():
    index = DataIndex()
    parser = EvidenceParser(index)
    parser.parse_all()
    print("Evidence parse summary:")
    for key, value in parser.summary().items():
        print(f"  {key}: {value}")

    # Quick demo: find evidence mentioning a known email
    demo_email = "keiko.nakamura@gmail.com"
    found = parser.records_by_identifier(normalize_email(demo_email))
    print(f"\nEvidence mentioning {demo_email}: {len(found)} records")
    for rec in found[:5]:
        print(f"  {rec['file_id']} | {rec['system_id']} | {rec['resource_id']} | {rec['data_category']} | {rec['state']}")


if __name__ == "__main__":
    main()
