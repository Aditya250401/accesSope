"""Small configuration/governance checks, not an AWS authorization evaluator."""
from .models import Finding, Snapshot


def evaluate(snapshot: Snapshot) -> list[Finding]:
    results = []
    roles = {r.arn: r for r in snapshot.roles}
    for workload in snapshot.workloads:
        context_ref = f"context:{workload.id}"

        def emit(rule, outcome, explanation, refs):
            results.append(Finding(id=f"{workload.id}:{rule}", workload_id=workload.id,
                                   rule=rule, outcome=outcome, explanation=explanation,
                                   evidence_ids=refs))

        # Missing imports and missing accountable owners are different conditions.
        if not snapshot.context_complete:
            emit("owner", "unknown", "Business-context coverage is incomplete.", [context_ref])
        elif workload.environment != "production":
            emit("owner", "not_applicable", "This owner rule applies to confirmed production workloads.", [context_ref])
        elif workload.owner and workload.owner_accepted:
            emit("owner", "satisfied", "An accountable owner has accepted ownership.", [context_ref])
        else:
            emit("owner", "finding", "A production workload lacks an accepted accountable owner.", [context_ref])

        if not snapshot.context_complete or workload.review_due is None:
            emit("review_due", "unknown", "A trustworthy review deadline is unavailable.", [context_ref])
        elif workload.review_due < snapshot.as_of:
            emit("review_due", "finding", "The recorded access-review deadline has passed.", [context_ref])
        else:
            emit("review_due", "satisfied", "The recorded review is not overdue as of the snapshot date.", [context_ref])

        role = roles.get(workload.role_arn)
        if not snapshot.iam_complete or role is None or not role.policy_coverage_complete:
            emit("wildcard_action", "unknown", "Role policy collection is incomplete.", [context_ref])
            continue
        broad_refs, unsupported = [], False
        for policy in role.policies:
            statements = policy.document.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]
            if not isinstance(statements, list):
                unsupported = True
                continue
            for statement in statements:
                if not isinstance(statement, dict):
                    unsupported = True
                    continue
                if statement.get("Effect") not in {"Allow", "Deny"} or not any(k in statement for k in ("Action", "NotAction")):
                    unsupported = True
                    continue
                if "NotAction" in statement:
                    unsupported = True
                actions = statement.get("Action", [])
                actions = [actions] if isinstance(actions, str) else actions
                if not isinstance(actions, list) or any(not isinstance(a, str) for a in actions):
                    unsupported = True
                    continue
                if statement.get("Effect") == "Allow" and "*" in actions:
                    broad_refs.append(policy.evidence_id)
        if broad_refs:
            emit("wildcard_action", "finding",
                 "A collected Allow statement contains Action '*'. Conditions, denies, boundaries and other controls may restrict it; effective access was not computed.",
                 sorted(set(broad_refs)))
        elif unsupported:
            emit("wildcard_action", "unknown", "Unsupported statement shapes prevent a complete result for this check.",
                 [p.evidence_id for p in role.policies])
        else:
            emit("wildcard_action", "satisfied", "No Action '*' Allow statement was found in the collected role policies; this does not establish least privilege.",
                 [p.evidence_id for p in role.policies])
    return results


def evidence_catalog(snapshot: Snapshot) -> dict:
    selected_roles = {w.role_arn for w in snapshot.workloads}
    return {
        **{p.evidence_id: {"source": p.source, "document": p.document} for r in snapshot.roles
           if r.arn in selected_roles for p in r.policies},
        **{f"context:{w.id}": {"source": "operator-supplied business context", **w.model_dump(mode="json")}
           for w in snapshot.workloads},
    }


def shared_role_consumers(snapshot: Snapshot) -> dict[str, list[str]]:
    bindings = {}
    for workload in snapshot.workloads:
        bindings.setdefault(workload.role_arn, []).append(workload.id)
    return {arn: sorted(ids) for arn, ids in bindings.items() if len(ids) > 1}
