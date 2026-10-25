"""Optional read-only AWS adapter. Injected SDK clients also allow offline checks."""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import unquote

from .models import Policy, Role, Snapshot, Workload


def policy_json(value):
    return value if isinstance(value, dict) else json.loads(unquote(value))


def collect(iam, lambda_client, *, account_id, region, contexts, context_complete):
    """Collect IAM policies and selected Lambda bindings; never read secrets or mutate AWS.

    contexts maps exact Lambda function ARNs to operator-supplied business context.
    Scope is one account and one Lambda region. Other workload providers are absent.
    """
    roles, managed, errors = [], {}, []
    iam_complete, workload_complete = True, True
    details = []
    try:
        for page in iam.get_paginator("get_account_authorization_details").paginate(
            Filter=["Role", "LocalManagedPolicy", "AWSManagedPolicy"]
        ):
            details.extend(page.get("RoleDetailList", []))
            for p in page.get("Policies", []):
                default = next((v for v in p.get("PolicyVersionList", []) if v.get("IsDefaultVersion")), None)
                if default is not None:
                    managed[p["Arn"]] = policy_json(default["Document"])
    except Exception:
        # Errors are surfaced as coverage, without serializing SDK exception bodies.
        iam_complete = False
        errors.append("IAM collection failed; inventory is partial.")
    for item in details:
        docs = [(p["PolicyName"], policy_json(p["PolicyDocument"])) for p in item.get("RolePolicyList", [])]
        complete = True
        for attached in item.get("AttachedManagedPolicies", []):
            if attached["PolicyArn"] not in managed:
                complete = False
            else:
                docs.append((attached["PolicyArn"], managed[attached["PolicyArn"]]))
        policies = []
        for name, doc in docs:
            digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]
            policies.append(Policy(evidence_id=f"policy:{item['RoleId']}:{name}:{digest}",
                                   source=f"{item['Arn']} / {name}", document=doc))
        roles.append(Role(arn=item["Arn"], policies=policies, policy_coverage_complete=complete))
    workloads = []
    try:
        for page in lambda_client.get_paginator("list_functions").paginate():
            for f in page.get("Functions", []):
                if f["FunctionArn"] not in contexts:
                    continue
                ctx = contexts[f["FunctionArn"]]
                workloads.append(Workload(id=f["FunctionArn"], role_arn=f["Role"], **ctx))
    except Exception:
        workload_complete = False
        errors.append("Lambda collection failed; workload inventory is partial.")
    observed = {w.id for w in workloads}
    if set(contexts) - observed:
        workload_complete = False
        errors.append("Some selected workload ARNs were not observed; absence is not confirmed deletion.")
    now = datetime.now(timezone.utc)
    return Snapshot(account_id=account_id, region=region, collected_at=now, as_of=now.date(),
                    iam_complete=iam_complete, workload_scan_complete=workload_complete,
                    context_complete=context_complete, collection_errors=errors, roles=roles, workloads=workloads)
