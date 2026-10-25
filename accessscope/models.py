"""Contracts separate collected configuration, business context, and conclusions."""
from datetime import date, datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(Contract):
    evidence_id: str
    source: str
    document: dict[str, Any]


class Role(Contract):
    arn: str
    policies: list[Policy]
    policy_coverage_complete: bool


class Workload(Contract):
    id: str
    role_arn: str
    environment: str
    owner: str | None
    owner_accepted: bool
    purpose: str
    review_due: date | None


class Snapshot(Contract):
    account_id: str = Field(pattern=r"^\d{12}$")
    region: str
    collected_at: datetime
    as_of: date
    iam_complete: bool
    workload_scan_complete: bool
    context_complete: bool
    collection_errors: list[str]
    roles: list[Role]
    workloads: list[Workload]

    @model_validator(mode="after")
    def unique_references(self):
        role_ids = [r.arn for r in self.roles]
        workload_ids = [w.id for w in self.workloads]
        evidence_ids = [p.evidence_id for r in self.roles for p in r.policies]
        for label, values in [("role", role_ids), ("workload", workload_ids), ("evidence", evidence_ids)]:
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label} identifiers")
        if any(not r.arn.startswith(f"arn:aws:iam::{self.account_id}:role/") for r in self.roles):
            raise ValueError("This prototype supports roles from its single selected AWS account")
        if any(not key or key.startswith("context:") for key in evidence_ids):
            raise ValueError("Policy evidence cannot use the reserved business-context namespace")
        return self

    def fingerprint(self) -> str:
        # A review is bound to this exact snapshot, including collection status.
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class Finding(Contract):
    id: str
    workload_id: str
    rule: str
    outcome: Literal["finding", "satisfied", "unknown", "not_applicable"]
    explanation: str
    evidence_ids: list[str]


class Brief(Contract):
    summary: str
    finding_ids: list[str]
    evidence_ids: list[str]
    reviewer_questions: list[str]


class Decision(Contract):
    action: Literal["retain", "investigate", "propose_change"]
    reviewer: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=3, max_length=1000)
    snapshot_fingerprint: str
