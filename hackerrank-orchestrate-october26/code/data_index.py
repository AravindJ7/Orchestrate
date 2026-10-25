#!/usr/bin/env python3
"""
Data index for the Ghost Data Still Exists deletion-audit agent.

Task 1 - Build data index.

Loads every participant-facing CSV from dataset/ into memory and builds the
in-memory lookup structures needed to resolve identities, apply deletion
policy, locate evidence, and reconstruct state at each case's audit_at time.

Structures built:
  * users_by_email            : normalized current email -> user_identities row
  * email_to_user_ids         : any known email (current or historical) -> user_ids
  * phone_to_user_ids / name_to_user_ids / email_hash_to_user_ids : same for other fields
  * user_identity_graph       : user_id -> {emails, phones, names, email_hashes, user_id}
                                collecting every identifier seen across the current
                                snapshot and the audit log (request email may be historical)
  * deletion_rules_by_category: data_category -> list of rule rows
  * deletion_rules_lookup     : (data_category, condition) -> rule row
  * activity_by_resource_id   : resource_id -> list of system_activity rows
  * activity_by_event_type    : event_type -> list of system_activity rows
  * activity_by_time          : sorted list of (datetime, activity_row) for time-range queries
  * evidence_by_id            : file_id -> evidence manifest row (with resolved full path)
  * evidence_by_path          : resolved full path -> file_id

Only participant-facing files under dataset/ are used. No organizer-only data.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path


def parse_ts(value):
    """Parse an ISO-8601 timestamp to a naive UTC datetime for comparison.

    Returns None when the value is missing or unparsable. Timestamps with a
    trailing 'Z' or an explicit offset are normalized to UTC (naive) so that
    comparison across mixed formats is sound.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def normalize_email(email):
    """Lowercase and trim an email; return None when empty."""
    if email is None:
        return None
    e = str(email).strip().lower()
    return e or None


def normalize_value(value):
    """Trim a general identifier value; return None when empty."""
    if value is None:
        return None
    v = str(value).strip()
    return v or None


class DataIndex:
    def __init__(self, dataset_dir=None):
        # Resolve paths relative to this file / repository root so it works on
        # macOS, Linux, and Windows without hardcoded user paths.
        if dataset_dir is None:
            script_dir = Path(__file__).resolve().parent
            repo_root = script_dir.parent
            dataset_dir = repo_root / "dataset"
        self.dataset_dir = Path(dataset_dir)
        self.repo_root = self.dataset_dir.parent

        # Raw loads (list of dict rows).
        self.deletion_requests = []
        self.user_identities = []
        self.user_audit_log = []
        self.deletion_rules = []
        self.system_activity = []
        self.evidence_files = []

        # Identity indexes.
        self.users_by_email = {}          # normalized current email -> user row
        self.email_to_user_ids = {}       # normalized email -> set(user_id)
        self.phone_to_user_ids = {}       # phone -> set(user_id)
        self.name_to_user_ids = {}        # name -> set(user_id)
        self.email_hash_to_user_ids = {}  # email_hash -> set(user_id)
        self.user_identity_graph = {}     # user_id -> identity graph dict

        # Deletion-rule indexes.
        self.deletion_rules_by_category = {}   # data_category -> [rule rows]
        self.deletion_rules_lookup = {}        # (data_category, condition) -> rule row

        # System-activity indexes.
        self.activity_by_resource_id = {}   # resource_id -> [activity rows]
        self.activity_by_event_type = {}    # event_type -> [activity rows]
        self.activity_by_time = []          # sorted [(dt, activity row), ...]

        # Evidence manifest indexes (paths resolved relative to dataset root).
        self.evidence_by_id = {}      # file_id -> manifest row (incl. _full_path, _exists)
        self.evidence_by_path = {}    # resolved full path -> file_id

        self.load_all()
        self.build_indexes()

    # ------------------------------------------------------------------ load
    def load_csv(self, filename):
        """Load a CSV from dataset/ into a list of dict rows."""
        path = self.dataset_dir / filename
        rows = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    def load_all(self):
        self.deletion_requests = self.load_csv("deletion_requests.csv")
        self.user_identities = self.load_csv("user_identities.csv")
        self.user_audit_log = self.load_csv("user_audit_log.csv")
        self.deletion_rules = self.load_csv("deletion_rules.csv")
        self.system_activity = self.load_csv("system_activity.csv")
        self.evidence_files = self.load_csv("evidence_files.csv")

    # ----------------------------------------------------------------- build
    def build_indexes(self):
        self._index_users_and_identity_graph()
        self._index_deletion_rules()
        self._index_system_activity()
        self._index_evidence_files()

    def _new_graph(self, user_id):
        return {
            "emails": set(),
            "phones": set(),
            "names": set(),
            "email_hashes": set(),
            "user_id": user_id,
        }

    def _index_users_and_identity_graph(self):
        """Index current snapshot + audit-log history into the identity graph."""
        # Current snapshot.
        for user in self.user_identities:
            user_id = user["user_id"]
            email = normalize_email(user.get("email"))
            phone = normalize_value(user.get("phone"))
            name = normalize_value(user.get("name"))
            email_hash = normalize_value(user.get("email_hash"))

            if email:
                self.users_by_email[email] = user

            graph = self.user_identity_graph.setdefault(user_id, self._new_graph(user_id))
            if email:
                graph["emails"].add(email)
            if phone:
                graph["phones"].add(phone)
            if name:
                graph["names"].add(name)
            if email_hash:
                graph["email_hashes"].add(email_hash)

        # Historical identifiers recovered from the audit log.
        for audit in self.user_audit_log:
            user_id = audit["user_id"]
            field_name = audit.get("field_name", "")
            old_value = audit.get("old_value")
            new_value = audit.get("new_value")

            graph = self.user_identity_graph.setdefault(user_id, self._new_graph(user_id))

            if field_name == "email":
                for val in (old_value, new_value):
                    n = normalize_email(val)
                    if n:
                        graph["emails"].add(n)
                        self.email_to_user_ids.setdefault(n, set()).add(user_id)
            elif field_name == "phone":
                for val in (old_value, new_value):
                    n = normalize_value(val)
                    if n:
                        graph["phones"].add(n)
                        self.phone_to_user_ids.setdefault(n, set()).add(user_id)
            elif field_name == "name":
                for val in (old_value, new_value):
                    n = normalize_value(val)
                    if n:
                        graph["names"].add(n)
                        self.name_to_user_ids.setdefault(n, set()).add(user_id)
            elif field_name == "email_hash":
                for val in (old_value, new_value):
                    n = normalize_value(val)
                    if n:
                        graph["email_hashes"].add(n)
                        self.email_hash_to_user_ids.setdefault(n, set()).add(user_id)

        # Fold current-snapshot identifiers into the reverse lookup maps so a
        # request email (current or historical) resolves to every matching user.
        for user_id, graph in self.user_identity_graph.items():
            for email in graph["emails"]:
                self.email_to_user_ids.setdefault(email, set()).add(user_id)
            for phone in graph["phones"]:
                self.phone_to_user_ids.setdefault(phone, set()).add(user_id)
            for name in graph["names"]:
                self.name_to_user_ids.setdefault(name, set()).add(user_id)
            for h in graph["email_hashes"]:
                self.email_hash_to_user_ids.setdefault(h, set()).add(user_id)

    def _index_deletion_rules(self):
        for rule in self.deletion_rules:
            cat = rule.get("data_category", "")
            cond = rule.get("condition", "")
            self.deletion_rules_by_category.setdefault(cat, []).append(rule)
            self.deletion_rules_lookup[(cat, cond)] = rule

    def _index_system_activity(self):
        for act in self.system_activity:
            rid = act.get("resource_id", "")
            if rid:
                self.activity_by_resource_id.setdefault(rid, []).append(act)
            et = act.get("event_type", "")
            if et:
                self.activity_by_event_type.setdefault(et, []).append(act)
            dt = parse_ts(act.get("event_time", ""))
            if dt is not None:
                self.activity_by_time.append((dt, act))
        self.activity_by_time.sort(key=lambda item: item[0])

    def _index_evidence_files(self):
        for ef in self.evidence_files:
            fid = ef.get("file_id")
            rel_path = ef.get("file_path", "")
            # Resolve relative to the dataset root so evidence/ paths work everywhere.
            full_path = (self.dataset_dir / rel_path).resolve()
            ef["_full_path"] = str(full_path)
            ef["_exists"] = full_path.exists()
            if fid:
                self.evidence_by_id[fid] = ef
                self.evidence_by_path[str(full_path)] = fid

    # --------------------------------------------------------------- queries
    def resolve_user_ids(self, request_email):
        """Return all user_ids whose known identifiers match request_email.

        Matches against current email, historical emails (audit log old/new
        values), and the full identity graph. Returns a set (possibly empty).
        """
        target = normalize_email(request_email)
        if not target:
            return set()
        user_ids = set()
        # Direct current-email match.
        if target in self.users_by_email:
            user_ids.add(self.users_by_email[target]["user_id"])
        # Reverse map from any known (current or historical) email.
        if target in self.email_to_user_ids:
            user_ids.update(self.email_to_user_ids[target])
        return user_ids

    def get_identity_graph(self, user_id):
        """Return the identity graph for a user_id, or None if unknown."""
        return self.user_identity_graph.get(user_id)

    def all_identifiers_for_user_ids(self, user_ids):
        """Collect every known identifier (emails/phones/names/hashes/ids) for users."""
        identifiers = set()
        for uid in user_ids:
            graph = self.user_identity_graph.get(uid)
            if not graph:
                continue
            identifiers.add(uid)
            identifiers.update(graph["emails"])
            identifiers.update(graph["phones"])
            identifiers.update(graph["names"])
            identifiers.update(graph["email_hashes"])
        return identifiers

    # ---------------------------------------------------------- deletion rules
    def get_rules_for_category(self, data_category):
        return list(self.deletion_rules_by_category.get(data_category, []))

    def get_rule(self, data_category, condition):
        return self.deletion_rules_lookup.get((data_category, condition))

    # --------------------------------------------------------- system activity
    def activities_for_resource(self, resource_id):
        return list(self.activity_by_resource_id.get(resource_id, []))

    def activities_by_event_type(self, event_type):
        return list(self.activity_by_event_type.get(event_type, []))

    def activities_in_time_range(self, start_dt, end_dt):
        """Activities whose event_time falls in [start_dt, end_dt] (inclusive)."""
        result = []
        for dt, act in self.activity_by_time:
            if start_dt is not None and dt < start_dt:
                continue
            if end_dt is not None and dt > end_dt:
                continue
            result.append(act)
        return result

    def activity_before(self, resource_id, audit_at):
        """Activities for a resource at or before audit_at."""
        cutoff = parse_ts(audit_at) if audit_at else None
        result = []
        for act in self.activity_by_resource_id.get(resource_id, []):
            dt = parse_ts(act.get("event_time", ""))
            if cutoff is None or (dt is not None and dt <= cutoff):
                result.append(act)
        return result

    # ---------------------------------------------------------------- evidence
    def evidence_for(self, file_id):
        return self.evidence_by_id.get(file_id)

    def evidence_captured_at_or_before(self, audit_at):
        """Return evidence manifest rows captured at or before audit_at.

        Sorted by captured_at. Rows with unparsable capture times are included
        at the end (permissive), mirroring the agent's evidence rule that only
        information available at or before audit_at may be used.
        """
        cutoff = parse_ts(audit_at) if audit_at else None
        with_time = []
        without_time = []
        for ef in self.evidence_files:
            dt = parse_ts(ef.get("captured_at", ""))
            if dt is None:
                without_time.append(ef)
            elif cutoff is None or dt <= cutoff:
                with_time.append((dt, ef))
        with_time.sort(key=lambda item: item[0])
        return [ef for _, ef in with_time] + without_time

    def summary(self):
        """Human-readable summary of index sizes (useful for logging/CLI)."""
        return {
            "deletion_requests": len(self.deletion_requests),
            "user_identities": len(self.user_identities),
            "user_audit_log": len(self.user_audit_log),
            "deletion_rules": len(self.deletion_rules),
            "system_activity": len(self.system_activity),
            "evidence_files": len(self.evidence_files),
            "users_by_email": len(self.users_by_email),
            "identity_graphs": len(self.user_identity_graph),
            "activity_resources": len(self.activity_by_resource_id),
            "evidence_resolved": len(self.evidence_by_id),
        }


def main():
    index = DataIndex()
    print("Data index built:")
    for key, value in index.summary().items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
