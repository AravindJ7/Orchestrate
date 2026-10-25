#!/usr/bin/env python3
"""
Audit engine for the Ghost Data Still Exists deletion-audit agent.

Task 3 - Case analysis, state reconstruction, rule application, step generation.

For every deletion request this engine:
  1. Determines whether request_text authorizes deletion (else NOT_APPLICABLE).
  2. Resolves the request email to user_ids (current + historical identifiers).
  3. Collects all evidence records relevant to those users at or before audit_at.
  4. Reconstructs the true state of each resource at audit_at (ignores
     reported_status / reported_result as proof).
  5. Applies deletion_rules.csv to every relevant record.
  6. Detects active data flows and restore risks.
  7. Generates ordered steps: FINDING -> REMEDIATION -> VERIFICATION.
  8. Returns deletion_status (COMPLETE / INCOMPLETE / NOT_APPLICABLE) and steps_json.

Step contract follows AGENTS.md §6.3/§6.4 exactly:
  * Findings cite participant-visible evidence_id.
  * Remediation and post-remediation verification use empty evidence_id.
  * COMPLETE cases include only evidence-backed verification steps.
  * Phases ordered FINDING, then REMEDIATION, then VERIFICATION.
  * Step numbers consecutive from 1.
"""

import csv
import json
import re
from pathlib import Path

from data_index import DataIndex, parse_ts, normalize_email, normalize_value
from evidence_parser import EvidenceParser


# ---------------------------------------------------------------------------
# Deletion authorization classifier (multilingual)
# ---------------------------------------------------------------------------

DELETION_KEYWORDS = [
    # English (prefix 'delet' catches delete/deleted/deletion/deleting)
    "delet", "erase", "erased", "erasure", "remove", "removed",
    "close my account", "close account", "closing my account", "closing account",
    "close this account", "closed my account",
    "permanently delete", "purge", "wipe", "forget me", "right to be forgotten",
    "account is gone", "account page is gone", "profile is gone",
    "disconnect", "disconnected", "disconnect my account",
    # Spanish
    "eliminar", "borrar", "suprimir", "eliminar mi cuenta", "borrar mi cuenta",
    # French
    "suppres", "effacer", "supprimer mon compte", "fermer mon compte",
    # German
    "l\u00f6schen", "konto l\u00f6schen", "konto schlie\u00dfen",
    # Portuguese
    "excluir", "apagar", "excluir minha conta",
    # Italian
    "eliminare", "cancellare", "elimina il mio account",
    # Dutch
    "verwijderen", "mijn account verwijderen",
    # Japanese
    "削除", "アカウントを削除", "アカウントを閉じる",
    # Korean
    "삭제", "계정 삭제", "계정을 삭제",
    # Arabic
    "حذف", "حذف حسابي",
    # Hindi
    "हटाएं", "खाता हटाएं",
]

NON_DELETION_KEYWORDS = [
    "access request", "access my data", "download your data", "export my data",
    "correct", "correction", "rectify", "rectification", "update my data",
    "opt out", "opt-out", "unsubscribe", "marketing", "newsletter",
    "copy of my data", "portable", "data portability",
]

STRONG_DELETION = [
    "delete my account", "close my account", "erase my account",
    "remove my account", "permanently delete", "delete the account",
    "close this account", "erase the account", "remove the account",
    "closing my account", "closing account", "closed my account",
    "account is closed", "my account is gone", "account page is gone",
    "disconnected my account",
    "supprimer mon compte", "eliminar mi cuenta", "borrar mi cuenta",
    "アカウントを削除", "계정 삭제", "حذف حسابي", "खाता हटाएं",
]

SPECIFIC_DELETION = [
    "delete my export", "delete the export", "remove my export",
    "delete my profile", "remove my profile", "delete personal data",
    "remove personal data", "erase personal data",
]


def is_deletion_authorized(request_text):
    """Return True if request_text actually authorizes account deletion."""
    if not request_text:
        return False
    text = request_text.lower()

    has_deletion = any(k in text for k in DELETION_KEYWORDS)
    has_strong = any(k in text for k in STRONG_DELETION)

    # Strong deletion signals always authorize deletion
    if has_strong:
        has_deletion = True

    if not has_deletion:
        return False

    has_non_deletion = any(k in text for k in NON_DELETION_KEYWORDS)
    if not has_non_deletion:
        return True

    # Both deletion and non-deletion keywords present
    if has_strong:
        return True
    if any(k in text for k in SPECIFIC_DELETION):
        return True
    # Co-occurrence: "delete ... export" means delete the export file
    if "delet" in text and ("export" in text or "download" in text):
        return True
    return False


# ---------------------------------------------------------------------------
# Rule -> step operation mapping
# ---------------------------------------------------------------------------

# required_action -> (finding_op, finding_state, rem_op, rem_state,
#                     ver_op, ver_state)
ACTION_TO_STEPS = {
    "DELETE": (
        "REPORT_REMAINING_RECORD", "PRESENT",
        "DELETE_RECORD", "ABSENT",
        "VERIFY_RECORD_ABSENT", "ABSENT",
    ),
    "REDACT": (
        "REPORT_EXPOSED_PII", "PRESENT",
        "REDACT_RECORD", "REDACTED",
        "VERIFY_RECORD_REDACTED", "REDACTED",
    ),
    "ANONYMIZE": (
        "REPORT_EXPOSED_PII", "PRESENT",
        "ANONYMIZE_RECORD", "ANONYMIZED",
        "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED",
    ),
    "RETAIN": (
        None, None, None, None,
        "VERIFY_RECORD_PRESENT", "PRESENT",
    ),
}

DATAFLOW_STEPS = (
    "REPORT_ACTIVE_DATA_FLOW", "ACTIVE",
    "DISABLE_DATA_FLOW", "DISABLED",
    "VERIFY_DATA_FLOW_DISABLED", "DISABLED",
)
RESTORE_STEPS = (
    "REPORT_RESTORE_RISK", "ACTIVE",
    "BLOCK_RESTORE", "BLOCKED",
    "VERIFY_RESTORE_BLOCKED", "BLOCKED",
)


def state_violates_required(current_state, required_action):
    """Return True if current_state does not satisfy required_action."""
    if required_action == "DELETE":
        return current_state in ("PRESENT", "FOUND", "ACTIVE", "IDENTIFIABLE")
    if required_action == "REDACT":
        return current_state in ("PRESENT", "FOUND", "IDENTIFIABLE", "ACTIVE")
    if required_action == "ANONYMIZE":
        return current_state in ("PRESENT", "FOUND", "IDENTIFIABLE", "ACTIVE")
    if required_action == "RETAIN":
        return current_state == "ABSENT"
    return False


# ---------------------------------------------------------------------------
# Audit engine
# ---------------------------------------------------------------------------

class AuditEngine:
    def __init__(self, data_index, evidence_parser):
        self.index = data_index
        self.parser = evidence_parser

    # ------------------------------------------------------------------ main
    def analyze_case(self, case):
        """Analyze a single deletion request case.

        Returns (deletion_status, steps) where steps is a list of step dicts.
        """
        request_text = case.get("request_text", "") or ""
        email = case.get("email", "") or ""
        audit_at = case.get("audit_at", "") or ""

        # 1. Deletion authorization
        if not is_deletion_authorized(request_text):
            return "NOT_APPLICABLE", []

        # 2. Resolve user_ids (current + historical)
        user_ids = self.index.resolve_user_ids(email)

        # 3. Gather evidence at or before audit_at
        evidence_records = self.parser.records_for_user_ids(user_ids, audit_at=audit_at)
        # Also search by request email directly (may not be in identity graph)
        direct_email = normalize_email(email)
        seen_keys = {(r["file_id"], r["resource_id"]) for r in evidence_records}
        if direct_email:
            for rec in self.parser.records_by_identifier(direct_email):
                key = (rec["file_id"], rec["resource_id"])
                if key in seen_keys:
                    continue
                cap = parse_ts(rec.get("captured_at", ""))
                cutoff = parse_ts(audit_at) if audit_at else None
                if cutoff is None or (cap is not None and cap <= cutoff):
                    evidence_records.append(rec)
                    seen_keys.add(key)

        # 4. Reconstruct state per resource (latest evidence at or before audit_at)
        resource_state = self._reconstruct_resource_state(evidence_records)

        # 5. Detect data flows and restore risks
        activity_flows = self._gather_data_flows(user_ids, email, audit_at)
        restore_risks = self._gather_restore_risks(user_ids, email, audit_at)

        # 6. Apply deletion rules and build steps
        findings = []
        remediation = []
        verification = []
        addressed_resources = set()

        for (system_id, resource_id), rec in resource_state.items():
            data_category = rec["data_category"]
            current_state = rec["state"]
            evidence_id = rec["file_id"]

            # Skip records we cannot attribute to a real system - these are
            # fallback mentions (e.g. log lines without a resource_id) and must
            # not generate findings.
            if system_id == "unknown":
                continue
            # Skip data flow / connector records - handled by
            # _gather_data_flows which uses DATAFLOW_STEPS instead.
            if system_id == "integration_service":
                continue
            # Skip backup/archive records - handled by _gather_restore_risks
            if system_id == "backup_service":
                continue
            # Account records are handled in a dedicated block below (we want a
            # single latest account verdict, not one step per stale account id).
            if system_id == "account_service" and data_category == "USER_PROFILE":
                continue

            addressed_resources.add(resource_id)

            rule = self.index.get_rule(data_category, "Always")
            if not rule:
                # Fall back to any rule for this category (conditions vary)
                cat_rules = self.index.get_rules_for_category(data_category)
                rule = cat_rules[0] if cat_rules else None
            required_action = rule["required_action"] if rule else "DELETE"

            steps = ACTION_TO_STEPS.get(required_action, ACTION_TO_STEPS["DELETE"])
            finding_op, finding_state, rem_op, rem_state, ver_op, ver_state = steps

            if state_violates_required(current_state, required_action):
                findings.append({
                    "phase": "FINDING",
                    "system_id": system_id,
                    "resource_id": resource_id,
                    "operation": finding_op,
                    "state": finding_state,
                    "evidence_id": evidence_id,
                })
                if rem_op:
                    remediation.append({
                        "phase": "REMEDIATION",
                        "system_id": system_id,
                        "resource_id": resource_id,
                        "operation": rem_op,
                        "state": rem_state,
                        "evidence_id": "",
                    })
                if ver_op:
                    verification.append({
                        "phase": "VERIFICATION",
                        "system_id": system_id,
                        "resource_id": resource_id,
                        "operation": ver_op,
                        "state": ver_state,
                        "evidence_id": "",
                    })
            else:
                # Compliant state - verification only (evidence-backed)
                if ver_op:
                    verification.append({
                        "phase": "VERIFICATION",
                        "system_id": system_id,
                        "resource_id": resource_id,
                        "operation": ver_op,
                        "state": ver_state,
                        "evidence_id": evidence_id,
                    })

        # 7. Data flow findings -> remediation -> verification
        for flow in activity_flows:
            findings.append({
                "phase": "FINDING",
                "system_id": flow["system_id"],
                "resource_id": flow["resource_id"],
                "operation": DATAFLOW_STEPS[0],
                "state": DATAFLOW_STEPS[1],
                "evidence_id": flow["evidence_id"],
            })
            remediation.append({
                "phase": "REMEDIATION",
                "system_id": flow["system_id"],
                "resource_id": flow["resource_id"],
                "operation": DATAFLOW_STEPS[2],
                "state": DATAFLOW_STEPS[3],
                "evidence_id": "",
            })
            verification.append({
                "phase": "VERIFICATION",
                "system_id": flow["system_id"],
                "resource_id": flow["resource_id"],
                "operation": DATAFLOW_STEPS[4],
                "state": DATAFLOW_STEPS[5],
                "evidence_id": "",
            })
            addressed_resources.add(flow["resource_id"])

        # 8. Restore risk findings -> remediation -> verification
        for risk in restore_risks:
            findings.append({
                "phase": "FINDING",
                "system_id": risk["system_id"],
                "resource_id": risk["resource_id"],
                "operation": RESTORE_STEPS[0],
                "state": RESTORE_STEPS[1],
                "evidence_id": risk["evidence_id"],
            })
            remediation.append({
                "phase": "REMEDIATION",
                "system_id": risk["system_id"],
                "resource_id": risk["resource_id"],
                "operation": RESTORE_STEPS[2],
                "state": RESTORE_STEPS[3],
                "evidence_id": "",
            })
            verification.append({
                "phase": "VERIFICATION",
                "system_id": risk["system_id"],
                "resource_id": risk["resource_id"],
                "operation": RESTORE_STEPS[4],
                "state": RESTORE_STEPS[5],
                "evidence_id": "",
            })
            addressed_resources.add(risk["resource_id"])

        # 9. Account-level: account_service should be ABSENT (verification).
        # We evaluate a single latest account record that matches the user
        # (stale account ids from older snapshots are ignored), then emit a
        # finding (record still PRESENT) or a verification (record ABSENT).
        account_verification = None
        account_finding = None
        account_remediation = None
        matched = self._matched_account_record(user_ids, email, evidence_records, audit_at)
        if matched:
            rec, evidence_id = matched
            current_state = rec["state"]
            resource_id = rec["resource_id"]
            if state_violates_required(current_state, "DELETE"):
                account_finding = {
                    "phase": "FINDING",
                    "system_id": "account_service",
                    "resource_id": resource_id,
                    "operation": "REPORT_REMAINING_RECORD",
                    "state": "PRESENT",
                    "evidence_id": evidence_id,
                }
                account_remediation = {
                    "phase": "REMEDIATION",
                    "system_id": "account_service",
                    "resource_id": resource_id,
                    "operation": "DELETE_RECORD",
                    "state": "ABSENT",
                    "evidence_id": "",
                }
            account_verification = {
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": resource_id,
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                # Evidence-backed only when the audit is complete (no findings);
                # otherwise this describes future work.
                "evidence_id": "" if (findings or account_finding) else evidence_id,
            }
        else:
            # No account evidence for this user: derive the account id from the
            # user id and verify absence.
            account_id = None
            if user_ids:
                try:
                    num = int(str(sorted(user_ids)[0]).rsplit("_", 1)[-1])
                    account_id = f"account_{num:03d}"
                except Exception:
                    account_id = None
            if account_id:
                account_verification = {
                    "phase": "VERIFICATION",
                    "system_id": "account_service",
                    "resource_id": account_id,
                    "operation": "VERIFY_RECORD_ABSENT",
                    "state": "ABSENT",
                    "evidence_id": "" if findings else self._latest_account_evidence(audit_at),
                }

        if account_finding:
            findings.insert(0, account_finding)
        if account_remediation:
            remediation.insert(0, account_remediation)
        if account_verification:
            verification.insert(0, account_verification)

        # 10. Security audit record retention (VERIFY_RECORD_PRESENT).
        audit_record = self._find_audit_record(user_ids, evidence_records, audit_at)
        if audit_record:
            addressed_resources.add(audit_record["resource_id"])
            ver_evidence = "" if findings else audit_record["file_id"]
            verification.append({
                "phase": "VERIFICATION",
                "system_id": audit_record["system_id"],
                "resource_id": audit_record["resource_id"],
                "operation": "VERIFY_RECORD_PRESENT",
                "state": "PRESENT",
                "evidence_id": ver_evidence,
            })

        # 11. Policy: remediation and post-remediation verification steps in an
        # INCOMPLETE case describe future work and must use an empty evidence_id.
        if findings:
            for step in remediation + verification:
                step["evidence_id"] = ""

        # 12. Order steps: FINDING -> REMEDIATION -> VERIFICATION
        steps = self._order_steps(findings, remediation, verification)

        # 13. Remove duplicate steps while preserving first-seen order
        deduped = []
        seen_steps = set()
        for step in steps:
            key = (
                step["phase"],
                step["system_id"],
                step["resource_id"],
                step["operation"],
                step["state"],
                step["evidence_id"],
            )
            if key in seen_steps:
                continue
            seen_steps.add(key)
            deduped.append(step)
        steps = deduped

        # 14. Assign step numbers
        for i, step in enumerate(steps, 1):
            step["step_no"] = i

        # 15. Determine status
        deletion_status = "INCOMPLETE" if findings else "COMPLETE"

        return deletion_status, steps

    # --------------------------------------------------------------- helpers
    def _reconstruct_resource_state(self, evidence_records):
        """Latest evidence record per (system_id, resource_id).

        When timestamps are equal, prefers the more terminal state
        (ABSENT over PRESENT, REDACTED over IDENTIFIABLE, etc.).
        """
        # State priority: higher = more terminal / resolved
        STATE_PRIORITY = {
            "ABSENT": 50,
            "ANONYMIZED": 45,
            "REDACTED": 40,
            "DISABLED": 35,
            "BLOCKED": 35,
            "PRESENT": 20,
            "IDENTIFIABLE": 15,
            "ACTIVE": 10,
            "FOUND": 20,
            "NOT_FOUND": 50,
        }

        best = {}
        for rec in evidence_records:
            key = (rec["system_id"], rec["resource_id"])
            if key not in best:
                best[key] = rec
                continue
            existing = best[key]
            ex_cap = parse_ts(existing.get("captured_at", ""))
            new_cap = parse_ts(rec.get("captured_at", ""))
            replace = False
            if new_cap is not None:
                if ex_cap is None or new_cap > ex_cap:
                    replace = True
                elif new_cap == ex_cap:
                    # Tiebreaker: more terminal state wins
                    ex_prio = STATE_PRIORITY.get(existing["state"], 0)
                    new_prio = STATE_PRIORITY.get(rec["state"], 0)
                    if new_prio > ex_prio:
                        replace = True
            if replace:
                best[key] = rec
        return best

    def _gather_data_flows(self, user_ids, email, audit_at):
        """Detect active data flows (integration_service connectors)."""
        flows = []
        cutoff = parse_ts(audit_at) if audit_at else None

        for file_id, records in self.parser.evidence_records.items():
            for rec in records:
                if rec["system_id"] != "integration_service":
                    continue
                cap = parse_ts(rec.get("captured_at", ""))
                if cutoff is not None and (cap is None or cap > cutoff):
                    continue
                raw = rec.get("raw", {})
                enabled = raw.get("enabled")
                status = raw.get("last_delivery_status", "")
                # A flow is active when enabled, delivering, or flagged ACTIVE.
                enabled_truthy = enabled in (True, 1, "1", "true", "yes") or str(
                    enabled).strip().lower() == "true"
                status_active = str(status).lower() in ("delivered", "active", "enabled", "success", "ok")
                if enabled_truthy or status_active or rec.get("state") == "ACTIVE":
                    subject = raw.get("customer_email") or raw.get("customer_reference") or ""
                    # Fall back to identifiers merged from correlated log lines
                    # (e.g. a lookup_started line carrying the user's email).
                    if not subject:
                        ids = rec.get("identifiers", {})
                        subject = (ids.get("emails") or [""])[0]
                    if self._identifier_matches(subject, user_ids, email):
                        flows.append({
                            "system_id": "integration_service",
                            "resource_id": rec["resource_id"],
                            "evidence_id": file_id,
                        })
        return flows

    def _gather_restore_risks(self, user_ids, email, audit_at):
        """Detect backup restore risks for the user."""
        risks = []
        cutoff = parse_ts(audit_at) if audit_at else None

        for file_id, records in self.parser.evidence_records.items():
            for rec in records:
                if rec["system_id"] != "backup_service":
                    continue
                cap = parse_ts(rec.get("captured_at", ""))
                if cutoff is not None and (cap is None or cap > cutoff):
                    continue
                raw = rec.get("raw", {})
                restore_allowed = raw.get("restore_allowed")
                state = rec.get("state", "")
                # A backup is a restore risk if restore is allowed OR the backup
                # state is ACTIVE (restore candidate).
                is_risk = (restore_allowed == 1 or restore_allowed == "1"
                           or state == "ACTIVE")
                # Subject from raw fields or from extracted identifiers
                subject = raw.get("customer_email") or raw.get("customer_reference") or ""
                if not subject:
                    ids = rec.get("identifiers", {})
                    subject = (ids.get("emails") or [""])[0]
                if is_risk and self._identifier_matches(subject, user_ids, email):
                    risks.append({
                        "system_id": "backup_service",
                        "resource_id": rec["resource_id"],
                        "evidence_id": file_id,
                    })
        return risks

    def _identifier_matches(self, value, user_ids, email):
        """Check if a value matches any known identifier for the users."""
        if not value:
            return False
        v = normalize_value(value)
        if not v:
            return False
        if v.lower() == normalize_email(email):
            return True
        for uid in user_ids:
            graph = self.index.get_identity_graph(uid)
            if graph:
                if (v in graph["emails"] or v in graph["phones"]
                        or v in graph["names"] or v in graph["email_hashes"]
                        or v == uid):
                    return True
        return False

    def _find_account_resource(self, user_ids, evidence_records, audit_at):
        """Find the account_service USER_PROFILE resource_id for these users."""
        cutoff = parse_ts(audit_at) if audit_at else None
        for rec in evidence_records:
            if (rec["system_id"] == "account_service"
                    and rec["data_category"] == "USER_PROFILE"):
                cap = parse_ts(rec.get("captured_at", ""))
                if cutoff is None or (cap is not None and cap <= cutoff):
                    ids = rec.get("identifiers", {})
                    rec_emails = set(ids.get("emails", []))
                    rec_user_ids = set(ids.get("user_ids", []))
                    for uid in sorted(user_ids):
                        graph = self.index.get_identity_graph(uid)
                        if graph:
                            if rec_emails & graph["emails"]:
                                return rec["resource_id"]
                            if rec_user_ids & {uid}:
                                return rec["resource_id"]
                    if rec_emails & {normalize_email(e) for e in [email] if e}:
                        return rec["resource_id"]
        # Fallback: any account_service USER_PROFILE record
        for rec in evidence_records:
            if rec["system_id"] == "account_service":
                return rec["resource_id"]
        return None

    def _matched_account_record(self, user_ids, email, evidence_records, audit_at):
        """Return (record, evidence_id) for the latest account_service
        USER_PROFILE record that matches the user (by email or user_id) and was
        captured at or before audit_at, or None if there is no such record.

        Stale account ids from older snapshots are ignored: only the most
        recent matching account record is considered.
        """
        cutoff = parse_ts(audit_at) if audit_at else None
        target_email = normalize_email(email)
        best = None  # (captured_at, state_priority, record, evidence_id)
        STATE_PRIORITY = {
            "ABSENT": 50, "ANONYMIZED": 45, "REDACTED": 40, "DISABLED": 35,
            "BLOCKED": 35, "PRESENT": 20, "IDENTIFIABLE": 15, "ACTIVE": 10,
            "FOUND": 20, "NOT_FOUND": 50,
        }
        for rec in evidence_records:
            if rec["system_id"] != "account_service" or rec["data_category"] != "USER_PROFILE":
                continue
            cap = parse_ts(rec.get("captured_at", ""))
            if cutoff is not None and (cap is None or cap > cutoff):
                continue
            ids = rec.get("identifiers", {})
            rec_emails = set(ids.get("emails", []))
            rec_user_ids = set(ids.get("user_ids", []))
            matched_user = False
            if target_email and target_email in rec_emails:
                matched_user = True
            for uid in user_ids:
                if uid in rec_user_ids:
                    matched_user = True
                    break
                graph = self.index.get_identity_graph(uid)
                if graph and rec_emails & graph["emails"]:
                    matched_user = True
                    break
            if not matched_user:
                continue
            prio = STATE_PRIORITY.get(rec["state"], 0)
            key = (cap or "", prio)
            if best is None or key > (best[0] or "", best[1]):
                best = (cap or "", prio, rec, rec["file_id"])
        if best is None:
            return None
        return best[2], best[3]

    def _find_audit_record(self, user_ids, evidence_records, audit_at):
        """Find a security_audit_records entry for these users."""
        cutoff = parse_ts(audit_at) if audit_at else None
        for rec in evidence_records:
            if rec["data_category"] == "SECURITY_AUDIT_LOG":
                cap = parse_ts(rec.get("captured_at", ""))
                if cutoff is None or (cap is not None and cap <= cutoff):
                    return rec
        return None

    def _evidence_for_resource(self, resource_id, audit_at):
        recs = self.parser.records_for_resource(resource_id, audit_at=audit_at)
        return recs[0]["file_id"] if recs else ""

    def _latest_account_evidence(self, audit_at):
        """Return the file_id of the latest account_service evidence captured
        at or before audit_at, or '' if none."""
        cutoff = parse_ts(audit_at) if audit_at else None
        best_fid = ""
        best_cap = None
        for rec in self.parser.evidence_records.values():
            for r in rec:
                if r["system_id"] != "account_service":
                    continue
                cap = parse_ts(r.get("captured_at", ""))
                if cutoff is not None and (cap is None or cap > cutoff):
                    continue
                if best_cap is None or (cap is not None and cap > best_cap):
                    best_cap = cap
                    best_fid = r["file_id"]
        return best_fid

    def _order_steps(self, findings, remediation, verification):
        """Order steps: FINDING -> REMEDIATION -> VERIFICATION.

        Within remediation: disable data flows and block restores before
        deleting/redacting dependent records.
        """
        ordered = list(findings)
        rem_priority = {
            "DISABLE_DATA_FLOW": 0,
            "BLOCK_RESTORE": 1,
            "DELETE_RECORD": 2,
            "REDACT_RECORD": 3,
            "ANONYMIZE_RECORD": 4,
        }
        remediation.sort(key=lambda s: rem_priority.get(s["operation"], 99))
        ordered.extend(remediation)
        ordered.extend(verification)
        return ordered


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

class DeletionAuditAgent:
    """Top-level agent that processes all deletion requests and writes output.csv."""

    def __init__(self, dataset_dir=None, output_dir=None):
        script_dir = Path(__file__).resolve().parent
        if dataset_dir is None:
            dataset_dir = script_dir.parent / "dataset"
        if output_dir is None:
            output_dir = script_dir.parent.parent

        self.dataset_dir = Path(dataset_dir)
        self.output_dir = Path(output_dir)

        self.index = DataIndex(self.dataset_dir)
        self.parser = EvidenceParser(self.index)
        self.engine = AuditEngine(self.index, self.parser)

        # Parse all evidence once (audit_at filtering happens at query time)
        self.parser.parse_all()

    def run(self):
        requests = sorted(self.index.deletion_requests, key=lambda r: r["case_id"])
        output_rows = []

        for case in requests:
            deletion_status, steps = self.engine.analyze_case(case)
            steps_json = json.dumps(steps, separators=(",", ":"))
            output_rows.append({
                "case_id": case["case_id"],
                "deletion_status": deletion_status,
                "steps_json": steps_json,
            })

        self._write_output(output_rows)
        return output_rows

    def _write_output(self, output_rows):
        output_path = self.output_dir / "output.csv"
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["case_id", "deletion_status", "steps_json"])
            for row in output_rows:
                writer.writerow([row["case_id"], row["deletion_status"], row["steps_json"]])
        print(f"Wrote {len(output_rows)} rows to {output_path}")


def main():
    agent = DeletionAuditAgent()
    agent.run()


if __name__ == "__main__":
    main()
