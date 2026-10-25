"""Reading stop 8 in WALKTHROUGH.md: replace the fixture with read-only AWS evidence.

Both this adapter and fixtures/demo.json feed the same Snapshot contract. Injected
SDK clients let the tests simulate paginated AWS responses without credentials.
"""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import unquote

from .models import Policy, Role, Snapshot, Workload


def policy_json(value):
    # Some policy documents arrive as dictionaries; encoded JSON needs decoding
    # before rules.py can inspect Statement/Action fields.
    return value if isinstance(value, dict) else json.loads(unquote(value))


def collect(iam, lambda_client, *, account_id, region, contexts, context_complete):
    """Collect IAM policies and selected Lambda bindings; never read secrets or mutate AWS.

    contexts maps exact Lambda function ARNs to operator-supplied business context.
    Scope is one account and one Lambda region. Other workload providers are absent.
    """
    roles, managed, errors = [], {}, []
    # Keep IAM coverage and Lambda coverage independent. We retain successful pages
    # when a later read fails, and label the result partial rather than complete.
    iam_complete, workload_complete = True, True
    details = []
    try:
        # A paginator continues beyond the first response. Collect ALL pages before
        # resolving role attachments: their managed documents may appear later.
        for page in iam.get_paginator("get_account_authorization_details").paginate(
            Filter=["Role", "LocalManagedPolicy", "AWSManagedPolicy"]
        ):
            details.extend(page.get("RoleDetailList", []))
            for p in page.get("Policies", []):
                # An attachment refers to an ARN; use its collected DEFAULT version,
                # not an arbitrary old version from PolicyVersionList.
                default = next((v for v in p.get("PolicyVersionList", []) if v.get("IsDefaultVersion")), None)
                if default is not None:
                    managed[p["Arn"]] = policy_json(default["Document"])
    except Exception:
        # Errors are surfaced as coverage, without serializing SDK exception bodies.
        iam_complete = False
        errors.append("IAM collection failed; inventory is partial.")
    for item in details:
        # Inline policies live directly on the role. Managed policies are separate
        # objects, so join attachments to the managed-document index built above.
        docs = [(p["PolicyName"], policy_json(p["PolicyDocument"])) for p in item.get("RolePolicyList", [])]
        complete = True
        for attached in item.get("AttachedManagedPolicies", []):
            if attached["PolicyArn"] not in managed:
                # We know an attachment exists but lack its contents. A "clean"
                # wildcard check would be unjustified, so mark this role incomplete.
                complete = False
            else:
                docs.append((attached["PolicyArn"], managed[attached["PolicyArn"]]))
        policies = []
        for name, doc in docs:
            # Include a content digest so changing the document changes this source
            # reference. This short ID hash is distinct from the full snapshot hash.
            digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]
            policies.append(Policy(evidence_id=f"policy:{item['RoleId']}:{name}:{digest}",
                                   source=f"{item['Arn']} / {name}", document=doc))
        roles.append(Role(arn=item["Arn"], policies=policies, policy_coverage_complete=complete))
    workloads = []
    try:
        for page in lambda_client.get_paginator("list_functions").paginate():
            for f in page.get("Functions", []):
                if f["FunctionArn"] not in contexts:
                    # Exact selected ARNs define this prototype's workload scope;
                    # seeing a function in the account does not select it for review.
                    continue
                ctx = contexts[f["FunctionArn"]]
                workloads.append(Workload(id=f["FunctionArn"], role_arn=f["Role"], **ctx))
    except Exception:
        workload_complete = False
        errors.append("Lambda collection failed; workload inventory is partial.")
    observed = {w.id for w in workloads}
    if set(contexts) - observed:
        # Requested-but-unseen can mean a partial scan or bad context. It is not
        # evidence of deletion, and we do not fabricate a collected Workload for it.
        workload_complete = False
        errors.append("Some selected workload ARNs were not observed; absence is not confirmed deletion.")
    now = datetime.now(timezone.utc)
    # AWS configuration supplies role bindings/policies. Ownership/purpose/deadlines
    # remain operator-supplied context; this collector cannot certify those facts.
    return Snapshot(account_id=account_id, region=region, collected_at=now, as_of=now.date(),
                    iam_complete=iam_complete, workload_scan_complete=workload_complete,
                    context_complete=context_complete, collection_errors=errors, roles=roles, workloads=workloads)
