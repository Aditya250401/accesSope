"""Exercise real agent routing with scripted provider boundaries; never call services."""
import json
from pathlib import Path
from types import SimpleNamespace
import socket

from langgraph.types import Command
import pytest

from accessscope.agent import TavilyResearch, build_agent_graph
from accessscope.models import Snapshot
from accessscope.rules import evaluate
from accessscope.workflow import DemoExplainer, explanation_input


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    def blocked(*args, **kwargs):
        raise AssertionError("Live network calls are not allowed in the regression suite")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def snapshot():
    return Snapshot.model_validate_json((Path(__file__).parents[1]/"fixtures/demo.json").read_text())


class Output:
    def __init__(self, value): self.value = value
    def model_dump(self, **kwargs): return self.value


class Responses:
    def __init__(self, actions):
        self.actions, self.calls = iter(actions), []
    def create(self, **kwargs):
        self.calls.append(kwargs)
        name, arguments = next(self.actions)
        return SimpleNamespace(output=[Output({"type": "function_call", "name": name,
              "arguments": json.dumps(arguments), "call_id": f"call-{len(self.calls)}"})],
              usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class Research:
    def __init__(self): self.calls = []
    def search(self, topic):
        self.calls.append(topic)
        return [{"url": "https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html",
                 "title": "Policy evaluation", "content": "A deterministic test snippet, not a live search."}]


def submission(snapshot, **overrides):
    brief = DemoExplainer().explain(explanation_input(snapshot, evaluate(snapshot))).model_dump(mode="json")
    return {"brief": brief, "source_ids": ["web:1"], **overrides}


def actions(snapshot):
    return [("get_review_findings", {}),
            ("get_evidence", {"evidence_id": "context:support-demo"}),
            ("get_evidence", {"evidence_id": "policy:shared-demo:v1"}),
            ("get_shared_consumers", {"role_arn": snapshot.workloads[0].role_arn}),
            ("search_aws_docs", {"topic": "policy_evaluation"}),
            ("submit_review", submission(snapshot))]


def test_real_agent_loop_reads_researches_and_pauses_without_repeating_calls(snapshot):
    responses, research = Responses(actions(snapshot)), Research()
    graph = build_agent_graph("gpt-4o", client=SimpleNamespace(responses=responses), research=research)
    config = {"configurable": {"thread_id": "agent-test"}, "recursion_limit": 40}
    result = graph.invoke({"snapshot": snapshot}, config)
    assert result["__interrupt__"] and len(responses.calls) == 6
    assert research.calls == ["policy_evaluation"]
    assert result["cited_source_ids"] == ["web:1"]
    assert result["usage"] == {"input_tokens": 60, "output_tokens": 30}
    assert all(c["model"] == "gpt-4o" and c["store"] is False for c in responses.calls)
    resumed = graph.invoke(Command(resume={"action": "investigate", "reviewer": "tester",
                           "reason": "Review the evidence", "snapshot_fingerprint": result["fingerprint"]}), config)
    assert resumed["decision"].action == "investigate" and len(responses.calls) == 6


@pytest.mark.parametrize("bad_action", [
    ("get_evidence", {"evidence_id": "outside-this-case"}),
    ("get_shared_consumers", {"role_arn": "arn:aws:iam::999900001111:role/foreign"}),
    ("search_aws_docs", {"topic": "send-private-role-document"}),
    ("delete_role", {}),
])
def test_tool_executor_rejects_unscoped_or_unknown_actions(snapshot, bad_action):
    responses = Responses([bad_action] + actions(snapshot))
    graph = build_agent_graph("gpt-4o", client=SimpleNamespace(responses=responses), research=Research())
    result = graph.invoke({"snapshot": snapshot}, {"configurable": {"thread_id": "rejected"}, "recursion_limit": 40})
    assert result["tool_log"][0]["ok"] is False
    assert result["finished"]


def test_unread_evidence_submission_is_rejected_and_model_can_correct(snapshot):
    responses = Responses([("get_review_findings", {}), ("submit_review", submission(snapshot, source_ids=[]))] + actions(snapshot))
    graph = build_agent_graph("gpt-4o", client=SimpleNamespace(responses=responses), research=Research())
    result = graph.invoke({"snapshot": snapshot}, {"configurable": {"thread_id": "unread"}, "recursion_limit": 40})
    assert result["tool_log"][1]["ok"] is False
    assert "Read the evidence" in result["tool_log"][1]["result"]["error"]


def test_turn_budget_stops_an_unfinished_agent(snapshot):
    graph = build_agent_graph("gpt-4o", max_turns=1,
              client=SimpleNamespace(responses=Responses([("get_review_findings", {})])), research=Research())
    with pytest.raises(ValueError, match="turn budget"):
        graph.invoke({"snapshot": snapshot}, {"configurable": {"thread_id": "budget"}})


def test_tavily_gets_only_fixed_public_queries_and_filters_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-placeholder")
    calls = []
    class Client:
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"results": [
                {"url": "https://attacker.example/fake", "content": "discard"},
                {"url": "https://docs.aws.amazon.com/IAM/guide", "title": "AWS", "content": "guide"}]})
    result = TavilyResearch(Client()).search("policy_evaluation")
    assert len(result) == 1 and result[0]["title"] == "AWS"
    assert calls[0][1]["json"]["include_domains"] == ["docs.aws.amazon.com"]
    assert "111122223333" not in calls[0][1]["json"]["query"]


def test_role_evidence_tool_batches_only_selected_policy_records(snapshot):
    flow = actions(snapshot)
    flow[2] = ("get_role_evidence", {"role_arn": snapshot.workloads[0].role_arn})
    graph = build_agent_graph("gpt-4o", client=SimpleNamespace(responses=Responses(flow)), research=Research())
    result = graph.invoke({"snapshot": snapshot}, {"configurable": {"thread_id": "batch"}, "recursion_limit": 40})
    assert result["tool_log"][2]["ok"]
    assert result["tool_log"][2]["result"]["policies"][0]["evidence_id"] == "policy:shared-demo:v1"


def test_database_import_redacts_identifiers_and_preserves_unknown_context(snapshot):
    from accessscope.import_iga import convert
    bundle = {
        "scope": {"workspace_id": "private-workspace", "execution_role_arn": snapshot.roles[0].arn,
                  "region": "us-east-1", "last_seen_generation": 1},
        "workloads": [{"id": "private-workload", "workspace_id": "private-workspace",
                       "execution_role_arn": snapshot.roles[0].arn, "stage": "unknown"}],
        "policies": [{"id": "private-policy", "policy_kind": "inline", "document_error": "",
                      "document": {"Statement": [{"Effect": "Allow", "Action": "*",
                                 "Resource": "arn:aws:s3:::private-bucket/*"}]}}],
        "unresolved_attachments": 0, "owners": [], "exported_at": "2026-10-26T09:00:00Z",
        "source_run": {"id": "private-run", "status": "published", "generation": 1,
                       "published_at": "2026-10-25T09:00:00Z", "coverage": {"surfaces": {
                       "iam_roles": {"state": "reached"}, "iam_policies": {"state": "reached"},
                       "lambda:us-east-1": {"state": "reached"}}}},
    }
    redacted, manifest = convert(bundle, b"test-pseudonym-salt")
    encoded = redacted.model_dump_json() + json.dumps(manifest)
    assert all(secret not in encoded for secret in ("private-workspace", "private-workload", "private-policy", "private-bucket", snapshot.roles[0].arn))
    assert redacted.iam_complete and not redacted.context_complete
    assert [f.outcome for f in evaluate(redacted)] == ["unknown", "unknown", "finding"]
    assert redacted.collected_at.date().isoformat() == "2026-10-25"


def test_local_project_tracing_does_not_keep_a_conflicting_agent_address(monkeypatch):
    from accessscope import settings
    import os
    monkeypatch.setattr(settings, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("LANGSMITH_PROJECT", "accessscope")
    monkeypatch.setenv("LANGSMITH_AGENT_ID", "reflexion")
    monkeypatch.setenv("LANGSMITH_AGENT_ENVIRONMENT", "local")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("CHAT_MODEL", "gpt-4o")
    monkeypatch.setenv("ACCESSSCOPE_AGENT_LABEL", "reflexion")
    monkeypatch.setenv("ACCESSSCOPE_ENVIRONMENT", "local")
    assert settings.load_settings() == "gpt-4o"
    assert "LANGSMITH_AGENT_ID" not in os.environ and "LANGSMITH_AGENT_ENVIRONMENT" not in os.environ
    assert os.environ["LANGSMITH_PROJECT"] == "accessscope"


def test_research_executor_enforces_two_search_budget(snapshot):
    flow = actions(snapshot)
    flow[-1:-1] = [("search_aws_docs", {"topic": "policy_evaluation"})] * 2
    research = Research()
    graph = build_agent_graph("gpt-4o", client=SimpleNamespace(responses=Responses(flow)), research=research)
    result = graph.invoke({"snapshot": snapshot}, {"configurable": {"thread_id": "search-budget"}, "recursion_limit": 40})
    assert len(research.calls) == 2
    assert result["tool_log"][-2]["ok"] is False
    assert "budget" in result["tool_log"][-2]["result"]["error"]
