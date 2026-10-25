"""Reading stop 2 in WALKTHROUGH.md: give each stage a dependable input/output.

The flow is Snapshot -> list[Finding] -> Brief -> Decision. These are Pydantic
objects, so constructing them parses/checks data. The graph's TypedDict in
workflow.py serves a different job: naming the fields carried between nodes.
"""
from datetime import date, datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    # All records inherit this rule. A misspelled field should raise an error
    # instead of being silently ignored at an evidence/review boundary.
    model_config = ConfigDict(extra="forbid")


class Policy(Contract):
    # evidence_id is how a finding/brief points back to this particular document.
    # document stays flexible JSON: rules.py supports only a narrow IAM pattern.
    evidence_id: str
    source: str
    document: dict[str, Any]


class Role(Contract):
    # A workload refers to this role by ARN. Several workloads can share it.
    # A missing attached policy makes this role's evidence incomplete.
    arn: str
    policies: list[Policy]
    policy_coverage_complete: bool


class Workload(Contract):
    # Think "a program whose access someone must review". AWS supplies the role
    # binding; accepted ownership, purpose and due date come from supplied context.
    id: str
    role_arn: str
    environment: str
    owner: str | None
    owner_accepted: bool
    purpose: str
    review_due: date | None


class Snapshot(Contract):
    # The account has 12 digits. This is a shape check, not proof of AWS origin.
    account_id: str = Field(pattern=r"^\d{12}$")
    region: str
    collected_at: datetime
    as_of: date
    # Collection time records when evidence was obtained. as_of is the date used
    # by the deadline rule, so replaying the fixture tomorrow gives the same result.
    # Separate flags stop an IAM outage from being confused with missing context.
    iam_complete: bool
    workload_scan_complete: bool
    context_complete: bool
    collection_errors: list[str]
    roles: list[Role]
    workloads: list[Workload]

    @model_validator(mode="after")
    def unique_references(self):
        # "after" means Pydantic has already built the fields. Now check relations
        # between fields: ambiguous IDs would make evidence citations unreliable.
        role_ids = [r.arn for r in self.roles]
        workload_ids = [w.id for w in self.workloads]
        evidence_ids = [p.evidence_id for r in self.roles for p in r.policies]
        for label, values in [("role", role_ids), ("workload", workload_ids), ("evidence", evidence_ids)]:
            # A set removes duplicates; unequal lengths therefore reveal one.
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label} identifiers")
        if any(not r.arn.startswith(f"arn:aws:iam::{self.account_id}:role/") for r in self.roles):
            raise ValueError("This prototype supports roles from its single selected AWS account")
        if any(not key or key.startswith("context:") for key in evidence_ids):
            # rules.py generates context:<workload-id>. Reserve that namespace so
            # a policy cannot overwrite a business-context entry in the catalog.
            raise ValueError("Policy evidence cannot use the reserved business-context namespace")
        return self

    def fingerprint(self) -> str:
        # A review is bound to this exact snapshot, including collection status.
        # mode="json" converts dates into JSON-compatible strings; sorted keys and
        # fixed separators make identical serialized contents produce the same hash.
        # This is change detection, not a signature or a fresh read from AWS.
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class Finding(Contract):
    # Despite the name, this records EVERY check outcome, including satisfied and
    # unknown. Only outcome="finding" means an active issue for model explanation.
    id: str
    workload_id: str
    rule: str
    outcome: Literal["finding", "satisfied", "unknown", "not_applicable"]
    explanation: str
    evidence_ids: list[str]


class Brief(Contract):
    # This is the explanation contract, not a second permission-policy evaluator.
    # Shape validation cannot prove the summary is supported by its citations.
    summary: str
    finding_ids: list[str]
    evidence_ids: list[str]
    reviewer_questions: list[str]


class Decision(Contract):
    # A review choice records human intent; even "propose_change" executes no write.
    # The fingerprint prevents submitting this choice against a different snapshot.
    action: Literal["retain", "investigate", "propose_change"]
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=1000)
    snapshot_fingerprint: str
