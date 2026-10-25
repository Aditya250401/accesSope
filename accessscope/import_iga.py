"""Convert a selected AuthSec database export into a redacted local case.

This accepts an explicitly scoped JSON export, not database credentials. The raw
bundle and pseudonym salt must remain private. Redacted policy values are useful
for narrow configuration review and must never be deployed as an IAM document.
"""
import argparse
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

from .models import Policy, Role, Snapshot, Workload


def write_private(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(content + "\n")


def convert(bundle, salt):
    scope = bundle.get("scope") or {}
    rows = bundle.get("workloads") or []
    if not rows or not scope:
        raise ValueError("No scoped workload/role records were exported")
    if len(rows) > 10:
        raise ValueError("Select at most 10 workloads for one investigation")
    if any(w["workspace_id"] != scope["workspace_id"] or w["execution_role_arn"] != scope["execution_role_arn"] for w in rows):
        raise ValueError("Selected bundle crosses workspace or chosen-role scope")

    def pseudonym(value):
        return hmac.new(salt, str(value).encode(), hashlib.sha256).hexdigest()[:12]

    safe_keys = {"Version", "Statement", "Sid", "Effect", "Action", "NotAction", "Resource", "NotResource", "Principal", "NotPrincipal", "Condition"}
    def redact(value, key=""):
        if isinstance(value, dict):
            return {(k if k in safe_keys or re.fullmatch(r"[A-Za-z]+(?::[A-Za-z]+)?", k) else "key-"+pseudonym(k)): redact(v, k) for k,v in value.items()}
        if isinstance(value, list):
            return [redact(v, key) for v in value]
        if isinstance(value, str):
            if value == "*" or (key == "Effect" and value in {"Allow", "Deny"}) or (key == "Version" and value in {"2012-10-17", "2008-10-17"}):
                return value
            if key in {"Action", "NotAction"} and re.fullmatch(r"[a-zA-Z0-9*-]+:[a-zA-Z0-9*?]+", value):
                return value
            return "redacted-" + pseudonym(value)
        if isinstance(value, int) and len(str(value)) == 12:
            return "redacted-" + pseudonym(value)
        return value

    account = str(int(pseudonym(scope["execution_role_arn"]), 16) % 10**12).zfill(12)
    role_arn = f"arn:aws:iam::{account}:role/role-{pseudonym(scope['execution_role_arn'])}"
    policies = []
    documents_complete = bundle.get("unresolved_attachments", 1) == 0
    for row in bundle.get("policies") or []:
        doc = row.get("document")
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except ValueError:
                doc = None
        if not isinstance(doc, dict) or row.get("document_error"):
            documents_complete = False
            continue
        policies.append(Policy(evidence_id="policy:"+pseudonym(row["id"]),
                               source="Redacted database policy / "+row["policy_kind"], document=redact(doc)))
    run = bundle.get("source_run") or {}
    coverage = run.get("coverage") or {}
    surfaces = coverage.get("surfaces") or {}
    iam_complete = (run.get("status") == "published" and run.get("generation") == scope.get("last_seen_generation")
                    and all(surfaces.get(k, {}).get("state") == "reached" for k in ("iam_roles", "iam_policies")))
    owners = {o["workload_id"]: o for o in bundle.get("owners") or []}
    workloads = []
    for row in rows:
        owner = owners.get(row["id"])
        workloads.append(Workload(id="workload-"+pseudonym(row["id"]), role_arn=role_arn,
            environment=row.get("stage") or "unknown", owner="owner-"+pseudonym(owner["user_id"]) if owner else None,
            owner_accepted=False, purpose="Business purpose not established by this database export.",
            review_due=(owner["review_due_at"][:10] if owner and owner.get("review_due_at") else None)))
    exported_at = datetime.fromisoformat(bundle["exported_at"].replace("Z", "+00:00"))
    source_time = coverage.get("finished_at") or run.get("published_at") or bundle["exported_at"]
    snapshot = Snapshot(account_id=account, region=scope["region"],
        collected_at=datetime.fromisoformat(source_time.replace("Z", "+00:00")), as_of=exported_at.date(),
        iam_complete=iam_complete,
        workload_scan_complete=surfaces.get("lambda:"+scope["region"], {}).get("state") == "reached",
        context_complete=False,
        collection_errors=["Database-derived snapshot; not a new AWS scan. Identifiers and policy resource/condition values are redacted.",
                           "Business-context completeness and owner acceptance are not established; preserve unknown outcomes."],
        roles=[Role(arn=role_arn, policies=policies, policy_coverage_complete=documents_complete)], workloads=workloads)
    manifest = {"source": "AuthSec IGA database, selected read-only export", "workspace": "workspace-"+pseudonym(scope["workspace_id"]),
                "source_run": "run-"+pseudonym(run.get("id", "unknown")), "source_status": run.get("status"),
                "source_generation": run.get("generation"), "source_published_at": run.get("published_at"),
                "exported_at": bundle["exported_at"], "workloads": len(workloads), "roles": 1, "policies": len(policies),
                "redactions": ["account/role/workload/user identifiers", "policy resource/condition strings", "policy display identifiers"],
                "limitations": ["No new source collection", "No complete effective-permission calculation", "No verified owner acceptance or business-purpose coverage"],
                "snapshot_fingerprint": snapshot.fingerprint()}
    return snapshot, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("private/authsec-snapshot.json"))
    args = parser.parse_args()
    salt_file = args.output.parent / ".pseudonym-salt"
    if not salt_file.exists():
        write_private(salt_file, os.urandom(32).hex())
    snapshot, manifest = convert(json.loads(args.bundle.read_text()), bytes.fromhex(salt_file.read_text().strip()))
    write_private(args.output, snapshot.model_dump_json(indent=2))
    write_private(args.output.with_name("snapshot-provenance.json"), json.dumps(manifest, indent=2))
    print(f"Created redacted database snapshot: {len(snapshot.workloads)} workloads, {len(snapshot.roles)} role, {len(snapshot.roles[0].policies)} policies.")


if __name__ == "__main__":
    main()
