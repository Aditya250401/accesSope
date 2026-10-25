"""Reading stop 9 in WALKTHROUGH.md: challenge the design one boundary at a time.

Read the tests in groups: evidence coverage -> policy shapes -> graph pause/resume
-> reference/review checks -> audit -> AWS/model adapters -> CLI integration.
Parametrization runs some functions with several inputs: 29 collected cases total.
"""
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
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    # autouse applies this to every test. "Live adapter" tests below use fake
    # clients; attempting a real connection in this suite raises immediately.
    def blocked(*args, **kwargs):
        raise AssertionError("No network is permitted in these checks")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def raw():
    # Each requesting test gets freshly loaded fixture data, so a mutation in one
    # case does not become another case's starting point.
    return json.loads((Path(__file__).parents[1]/"fixtures/demo.json").read_text())


@pytest.mark.parametrize("coverage", ["iam_complete", "context_complete"])
def test_missing_coverage_cannot_become_satisfied(raw, coverage):
    # Lose one evidence source; only its dependent rule family must become unknown.
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
    # Not understanding a construct is different from proving the pattern absent.
    raw["roles"][0]["policies"][0]["document"]["Statement"] = [statement]
    findings = evaluate(Snapshot.model_validate(raw))
    assert all(f.outcome == "unknown" for f in findings if f.rule == "wildcard_action")


def test_configured_wildcard_does_not_claim_effective_access(raw):
    # The wildcard is still configured with a Condition; wording must stay narrow.
    raw["roles"][0]["policies"][0]["document"]["Statement"][0]["Condition"] = {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
    findings = evaluate(Snapshot.model_validate(raw))
    assert all("effective access was not computed" in f.explanation for f in findings if f.rule == "wildcard_action")


def test_single_statement_object_supported(raw):
    # Normalization should accept IAM's single-object representation too.
    doc = raw["roles"][0]["policies"][0]["document"]
    doc["Statement"] = doc["Statement"][0]
    assert any(f.outcome == "finding" and f.rule == "wildcard_action" for f in evaluate(Snapshot.model_validate(raw)))


@pytest.mark.parametrize("mutation", ["duplicate_role", "duplicate_evidence", "wrong_account", "reserved_namespace"])
def test_ambiguous_or_cross_scope_records_rejected(raw, mutation):
    # Malformed evidence identity should fail at the contract, before graph work.
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
    # Otherwise a reviewer could overlook impact on the second selected workload.
    assert list(shared_role_consumers(Snapshot.model_validate(raw)).values()) == [["billing-demo", "support-demo"]]


class Spy(DemoExplainer):
    # A counter around scripted text, NOT a real API adapter. live=True exercises
    # the graph's logical call counter without spending money or sending evidence.
    live = True
    def __init__(self): self.calls = 0
    def explain(self, payload):
        self.calls += 1
        return super().explain(payload)


def started(raw, spy=None):
    # Common arrange step: run a real graph as far as its review interrupt.
    graph = build_graph(spy)
    config = {"configurable": {"thread_id": "test-case"}}
    state = graph.invoke({"snapshot": Snapshot.model_validate(raw)}, config)
    return graph, config, state


def review_payload(state, **updates):
    # Valid baseline + one deliberate change makes rejection cases easy to read.
    return {"action": "investigate", "reviewer": "local-tester", "reason": "Needs owner review",
            "snapshot_fingerprint": state["fingerprint"], **updates}


def test_graph_pauses_and_resume_does_not_repeat_model_work(raw):
    # Before resume: no decision. After resume: decision present, counter unchanged.
    spy = Spy()
    graph, config, state = started(raw, spy)
    assert state["__interrupt__"] and "decision" not in state and spy.calls == 1
    end = graph.invoke(Command(resume=review_payload(state)), config)
    assert end["decision"].action == "investigate" and spy.calls == 1
    assert end["model_calls"] == 1


@pytest.mark.parametrize("change", [{"snapshot_fingerprint": "stale"}, {"action": "delete_role"}, {"reason": ""}])
def test_stale_or_invalid_review_cannot_complete(raw, change):
    # Either contract validation or snapshot comparison must reject these inputs.
    graph, config, state = started(raw)
    with pytest.raises(ValueError):
        graph.invoke(Command(resume=review_payload(state, **change)), config)


@pytest.mark.parametrize("change", [{"finding_ids": ["invented"]}, {"evidence_ids": ["invented"]}, {"finding_ids": []}, {"evidence_ids": []}])
def test_model_reference_errors_are_rejected(raw, change):
    # Start with a valid scripted brief, then invent/omit a required reference.
    snap = Snapshot.model_validate(raw)
    findings = evaluate(snap)
    from accessscope.workflow import explanation_input
    brief = DemoExplainer().explain(explanation_input(snap, findings)).model_dump()
    brief.update(change)
    with pytest.raises(ValueError): validate_brief(brief, snap, findings)


def test_snapshot_change_has_a_different_review_fingerprint(raw):
    # This proves hash sensitivity to a changed field, not live-state revalidation.
    first = Snapshot.model_validate(raw).fingerprint()
    raw["workloads"][0]["owner"] = "New owner"
    assert Snapshot.model_validate(raw).fingerprint() != first


def test_audit_is_idempotent_and_conflicting_decision_rejected(raw, tmp_path):
    # Retry the exact payload -> one row. Change the decision for that pair -> error.
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
    # Tiny paginator fake: the collector consumes these pages just as it would
    # iterate SDK responses. Later-page behavior stays testable without AWS.
    def __init__(self, pages): self.pages = pages
    def paginate(self, **kwargs):
        yield from self.pages


class SDK:
    # Only implements the method the collector needs; it is not an AWS emulator.
    def __init__(self, pages): self.pages = pages
    def get_paginator(self, operation): return Pages(self.pages)


def test_collection_visits_later_pages_and_tracks_missing_targets():
    # Put useful data on page TWO, then repeat with the selected Lambda missing.
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
    # A denied IAM read must remain visible in coverage and collection errors.
    class Denied:
        def get_paginator(self, operation): raise RuntimeError("denied")
    snap = collect(Denied(), SDK([]), account_id="111122223333", region="us-east-1", contexts={}, context_complete=False)
    assert not snap.iam_complete and snap.collection_errors


def test_live_adapter_uses_schema_no_storage_and_bounded_single_request():
    # Record request arguments with a fake parse(). This checks adapter wiring,
    # not provider behavior, output quality, retention guarantees or real latency.
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
    # Exercise the actual CLI twice, with temporary output and changed reasons.
    # Default UUIDs must keep the two reviews from conflicting in the audit table.
    from accessscope.cli import main
    import sys
    out = tmp_path/"review"
    for reason in ["First local review", "Second local review"]:
        monkeypatch.setattr(sys, "argv", ["accessscope", "--demo", "--decision", "investigate", "--output", str(out), "--reason", reason])
        main()
    with sqlite3.connect(out/"reviews.db") as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 2
    assert json.loads((out/"review.json").read_text())["case_id"]


def test_unselected_role_policies_are_not_sent_to_model(raw):
    # Evidence selection should follow the selected workload-role bindings.
    from accessscope.rules import evidence_catalog
    other = copy.deepcopy(raw["roles"][0])
    other["arn"] = "arn:aws:iam::111122223333:role/unselected"
    other["policies"][0]["evidence_id"] = "unselected-evidence"
    raw["roles"].append(other)
    assert "unselected-evidence" not in evidence_catalog(Snapshot.model_validate(raw))


def test_oversized_input_stops_before_a_model_call():
    # The fake raises if called: passing means the input guard ran FIRST.
    def must_not_run(**kwargs): raise AssertionError("Unexpected API call")
    adapter = OpenAIExplainer("user-selected-model", SimpleNamespace(responses=SimpleNamespace(parse=must_not_run)))
    with pytest.raises(ValueError, match="budget"):
        adapter.explain({"untrusted_text": "x"*25000})
