import copy
import json
from pathlib import Path
import sqlite3
import socket
from types import SimpleNamespace

from langgraph.types import Command
import pytest

from accessscope.collect import collect
from accessscope.models import Brief, Snapshot
from accessscope.rules import evaluate, shared_role_consumers
from accessscope.workflow import DemoExplainer, OpenAIExplainer, build_graph, record_decision, validate_brief


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("No network is permitted in these checks")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parents[1]/"fixtures/demo.json").read_text())


@pytest.mark.parametrize("coverage", ["iam_complete", "context_complete"])
def test_missing_coverage_cannot_become_satisfied(raw, coverage):
    raw[coverage] = False
    findings = evaluate(Snapshot.model_validate(raw))
    affected = {"wildcard_action"} if coverage == "iam_complete" else {"owner", "review_due"}
    assert all(f.outcome == "unknown" for f in findings if f.rule in affected)


@pytest.mark.parametrize("statement", [
    {"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"},
    {"Effect": "Allow", "Action": [42], "Resource": "*"},
    {"Action": "s3:GetObject", "Resource": "*"},
    "unexpected",
])
def test_unsupported_policy_shapes_are_unknown(raw, statement):
    raw["roles"][0]["policies"][0]["document"]["Statement"] = [statement]
    findings = evaluate(Snapshot.model_validate(raw))
    assert all(f.outcome == "unknown" for f in findings if f.rule == "wildcard_action")


def test_configured_wildcard_does_not_claim_effective_access(raw):
    raw["roles"][0]["policies"][0]["document"]["Statement"][0]["Condition"] = {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
    findings = evaluate(Snapshot.model_validate(raw))
    assert all("effective access was not computed" in f.explanation for f in findings if f.rule == "wildcard_action")


def test_single_statement_object_supported(raw):
    doc = raw["roles"][0]["policies"][0]["document"]
    doc["Statement"] = doc["Statement"][0]
    assert any(f.outcome == "finding" and f.rule == "wildcard_action" for f in evaluate(Snapshot.model_validate(raw)))


@pytest.mark.parametrize("mutation", ["duplicate_role", "duplicate_evidence", "wrong_account", "reserved_namespace"])
def test_ambiguous_or_cross_scope_records_rejected(raw, mutation):
    if mutation == "duplicate_role":
        raw["roles"].append(copy.deepcopy(raw["roles"][0]))
    elif mutation == "duplicate_evidence":
        raw["roles"][0]["policies"].append(copy.deepcopy(raw["roles"][0]["policies"][0]))
    elif mutation == "wrong_account":
        raw["account_id"] = "999900001111"
    else:
        raw["roles"][0]["policies"][0]["evidence_id"] = "context:support-demo"
    with pytest.raises(ValueError):
        Snapshot.model_validate(raw)


def test_shared_role_keeps_both_consumers(raw):
    assert list(shared_role_consumers(Snapshot.model_validate(raw)).values()) == [["billing-demo", "support-demo"]]


class Spy(DemoExplainer):
    live = True
    def __init__(self): self.calls = 0
    def explain(self, payload):
        self.calls += 1
        return super().explain(payload)


def started(raw, spy=None):
    graph = build_graph(spy)
    config = {"configurable": {"thread_id": "test-case"}}
    state = graph.invoke({"snapshot": Snapshot.model_validate(raw)}, config)
    return graph, config, state


def review_payload(state, **updates):
    return {"action": "investigate", "reviewer": "local-tester", "reason": "Needs owner review",
            "snapshot_fingerprint": state["fingerprint"], **updates}


def test_graph_pauses_and_resume_does_not_repeat_model_work(raw):
    spy = Spy()
    graph, config, state = started(raw, spy)
    assert state["__interrupt__"] and "decision" not in state and spy.calls == 1
    end = graph.invoke(Command(resume=review_payload(state)), config)
    assert end["decision"].action == "investigate" and spy.calls == 1
    assert end["model_calls"] == 1


@pytest.mark.parametrize("change", [{"snapshot_fingerprint": "stale"}, {"action": "delete_role"}, {"reason": ""}])
def test_stale_or_invalid_review_cannot_complete(raw, change):
    graph, config, state = started(raw)
    with pytest.raises(ValueError):
        graph.invoke(Command(resume=review_payload(state, **change)), config)


@pytest.mark.parametrize("change", [{"finding_ids": ["invented"]}, {"evidence_ids": ["invented"]}, {"finding_ids": []}, {"evidence_ids": []}])
def test_model_reference_errors_are_rejected(raw, change):
    snap = Snapshot.model_validate(raw)
    findings = evaluate(snap)
    from accessscope.workflow import explanation_input
    brief = DemoExplainer().explain(explanation_input(snap, findings)).model_dump()
    brief.update(change)
    with pytest.raises(ValueError): validate_brief(brief, snap, findings)


def test_snapshot_change_has_a_different_review_fingerprint(raw):
    first = Snapshot.model_validate(raw).fingerprint()
    raw["workloads"][0]["owner"] = "New owner"
    assert Snapshot.model_validate(raw).fingerprint() != first


def test_audit_is_idempotent_and_conflicting_decision_rejected(raw, tmp_path):
    graph, config, state = started(raw)
    end = graph.invoke(Command(resume=review_payload(state)), config)
    db = tmp_path/"audit.db"
    record_decision(db, "case", end)
    record_decision(db, "case", end)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
    end["decision"] = end["decision"].model_copy(update={"action": "retain"})
    with pytest.raises(ValueError): record_decision(db, "case", end)


class Pages:
    def __init__(self, pages): self.pages = pages
    def paginate(self, **kwargs):
        yield from self.pages


class SDK:
    def __init__(self, pages): self.pages = pages
    def get_paginator(self, operation): return Pages(self.pages)


def test_collection_visits_later_pages_and_tracks_missing_targets():
    arn = "arn:aws:lambda:us-east-1:111122223333:function:example"
    role_arn = "arn:aws:iam::111122223333:role/example"
    iam = SDK([{}, {"RoleDetailList": [{"Arn": role_arn, "RoleId": "stable-id", "RolePolicyList": []}]}])
    lambdas = SDK([{}, {"Functions": [{"FunctionArn": arn, "Role": role_arn}]}])
    context = {arn: {"environment": "production", "owner": None, "owner_accepted": False,
                     "purpose": "fixture", "review_due": None}}
    snap = collect(iam, lambdas, account_id="111122223333", region="us-east-1", contexts=context, context_complete=True)
    assert len(snap.workloads) == len(snap.roles) == 1 and snap.workload_scan_complete
    snap = collect(iam, SDK([{}]), account_id="111122223333", region="us-east-1", contexts=context, context_complete=True)
    assert not snap.workload_scan_complete and snap.collection_errors


def test_unavailable_iam_is_partial_not_a_clean_scan():
    class Denied:
        def get_paginator(self, operation): raise RuntimeError("denied")
    snap = collect(Denied(), SDK([]), account_id="111122223333", region="us-east-1", contexts={}, context_complete=False)
    assert not snap.iam_complete and snap.collection_errors


def test_live_adapter_uses_schema_no_storage_and_bounded_single_request():
    calls = []
    expected = Brief(summary="Fixture", finding_ids=[], evidence_ids=[], reviewer_questions=[])
    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_parsed=expected)
    adapter = OpenAIExplainer("user-selected-model", SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    assert adapter.explain({"findings": []}) == expected
    assert len(calls) == 1 and calls[0]["store"] is False
    assert calls[0]["text_format"] is Brief and calls[0]["max_output_tokens"] == 1500


def test_cli_repeated_demo_runs_start_separate_cases(tmp_path, monkeypatch):
    from accessscope.cli import main
    import sys
    out = tmp_path/"review"
    for reason in ["First local review", "Second local review"]:
        monkeypatch.setattr(sys, "argv", ["accessscope", "--decision", "investigate", "--output", str(out), "--reason", reason])
        main()
    with sqlite3.connect(out/"reviews.db") as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 2
    assert json.loads((out/"review.json").read_text())["case_id"]


def test_unselected_role_policies_are_not_sent_to_model(raw):
    from accessscope.rules import evidence_catalog
    other = copy.deepcopy(raw["roles"][0])
    other["arn"] = "arn:aws:iam::111122223333:role/unselected"
    other["policies"][0]["evidence_id"] = "unselected-evidence"
    raw["roles"].append(other)
    assert "unselected-evidence" not in evidence_catalog(Snapshot.model_validate(raw))


def test_oversized_input_stops_before_a_model_call():
    def must_not_run(**kwargs): raise AssertionError("Unexpected API call")
    adapter = OpenAIExplainer("user-selected-model", SimpleNamespace(responses=SimpleNamespace(parse=must_not_run)))
    with pytest.raises(ValueError, match="budget"):
        adapter.explain({"untrusted_text": "x"*25000})
