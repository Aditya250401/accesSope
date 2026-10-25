"""Reading stop 7 in WALKTHROUGH.md: connect input, graph execution and saved files.

uv run python -m accessscope.cli --decision investigate
The default runs a real tool-calling agent. Use --demo for the offline lesson.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import uuid

from langgraph.types import Command

from .models import Snapshot
from .workflow import build_graph, record_decision
from .settings import load_settings, require_agent_credentials


def main():
    # Input source and runtime mode are separate choices. Normal operation uses
    # the real agent; --demo explicitly selects the offline fixed-graph lesson.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/latest"))
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Explicit offline scripted lesson; never silently selected")
    parser.add_argument("--task", default="Review this snapshot, inspect the supporting evidence and shared-role impact, and research relevant AWS documentation before submitting a brief.")
    parser.add_argument("--decision", choices=["retain", "investigate", "propose_change"])
    parser.add_argument("--reviewer", default="local-demo-reviewer")
    parser.add_argument("--reason", default="Inspect the evidence before proposing any source change.")
    parser.add_argument("--case-id", help="Reuse an explicit ID to retry the same review; a new run otherwise starts a new case")
    parser.add_argument("--collect-aws", action="store_true")
    parser.add_argument("--contexts", type=Path)
    parser.add_argument("--region")
    parser.add_argument("--profile", help="Explicit AWS profile for --collect-aws")
    args = parser.parse_args()
    # Offline lessons do not load credentials or enable outbound tracing.
    model = None if args.demo else load_settings()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.collect_aws:
        if not args.contexts or not args.region:
            parser.error("AWS collection requires --contexts and --region")
        import boto3
        from .collect import collect
        # boto3 resolves local AWS credentials. The collector reads configuration;
        # the supplied contexts add ownership/purpose AWS cannot establish for us.
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        account = session.client("sts").get_caller_identity()["Account"]
        context = json.loads(args.contexts.read_text())
        snapshot = collect(session.client("iam"), session.client("lambda"), account_id=account,
                           region=args.region, contexts=context["workloads"], context_complete=context["complete"])
    else:
        # Parse JSON into the same Snapshot contract that live collection returns.
        # The downstream rules/graph do not need to know which adapter supplied it.
        snapshot_file = args.snapshot or Path(__file__).resolve().parents[1] / ("fixtures/demo.json" if args.demo else "private/authsec-snapshot.json")
        if not snapshot_file.is_file():
            parser.error("Provide --snapshot with a database/AWS snapshot, or use --demo for the offline lesson")
        snapshot = Snapshot.model_validate_json(snapshot_file.read_text())
    snapshot_path = args.output/"snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2)+"\n")
    # Saving input lets a reviewer inspect what this run actually used.
    if args.demo and args.live_model:
        parser.error("Choose --demo or live operation, not both")
    if args.demo:
        # Offline lessons remain deliberate and cannot masquerade as a live run.
        graph = build_graph()
    else:
        from .agent import build_agent_graph
        try:
            require_agent_credentials()
        except ValueError as exc:
            parser.error(str(exc))
        graph = build_agent_graph(model)
    case_id = args.case_id or str(uuid.uuid4())
    # case_id names this review; thread_id tells the checkpointer which execution
    # to resume. They share a value here, but neither authenticates the reviewer.
    run_id = uuid.uuid4()
    config = {"configurable": {"thread_id": case_id}, "run_id": run_id, "recursion_limit": 40,
              "metadata": {"application": "accessscope", "environment": os.environ.get("ACCESSSCOPE_ENVIRONMENT", "local"),
                           "agent_label": os.environ.get("ACCESSSCOPE_AGENT_LABEL", "accessscope")}}
    print("Mode:", "SCRIPTED DEMO (no model call)" if args.demo else f"LIVE AGENT ({model})", flush=True)
    # Node updates expose what the real agent chose while it works; no credentials
    # are part of these events or the persisted graph state.
    for event in graph.stream({"snapshot": snapshot, "task": args.task}, config, stream_mode="updates"):
        if "tools" in event:
            entry = event["tools"]["tool_log"][-1]
            print("Tool:", entry["name"], "OK" if entry["ok"] else "REJECTED", flush=True)
    result = graph.get_state(config).values
    paused = bool(graph.get_state(config).next)
    # The streamed run prepares outcomes, loops agent/tools until submission, and
    # pauses at review. The demo instead uses its fixed explain/validate chain.
    # No decision exists yet, even when --decision will supply it below.
    print("AWS writes: none; effective access: not evaluated")
    print(result["brief"].summary)
    for finding in result["findings"]:
        print(f"  {finding.outcome:14} {finding.id}: {finding.explanation}")
    # Save the investigation before prompting, so evidence is inspectable even if
    # the terminal closes while waiting for the human decision.
    investigation = {"mode": "scripted_demo" if args.demo else "live_agent", "model": None if args.demo else model,
                     "trace_run_id": str(run_id) if not args.demo else None,
                     "trace_verified": False,
                     "model_calls": result["model_calls"], "tool_log": result.get("tool_log", []),
                     "research_sources": result.get("research_sources", []),
                     "cited_source_ids": result.get("cited_source_ids", []),
                     "usage": result.get("usage", {}), "brief": result["brief"].model_dump(mode="json")}
    (args.output/"investigation.json").write_text(json.dumps(investigation, indent=2)+"\n")
    if paused:
        # input() is the CLI waiting for a person. interrupt() was the GRAPH pause.
        # --decision is a convenient way to supply the choice without typing it.
        action = args.decision or input("Review choice [retain/investigate/propose_change]: ").strip()
        result = graph.invoke(Command(resume={"action": action, "reviewer": args.reviewer,
                                             "reason": args.reason, "snapshot_fingerprint": snapshot.fingerprint()}),
                              {**config, "run_id": uuid.uuid4()})
        # Same graph + same thread -> restart review; resume data is returned by
        # its interrupt() call. Earlier explanation work is not repeated here.
    # Completion, audit storage and report export are separate responsibilities.
    record_decision(args.output/"reviews.db", case_id, result)
    report = {**investigation, "aws_writes": False,
              "case_id": case_id, "effective_access": "not_evaluated", "snapshot_fingerprint": result["fingerprint"],
              "model_calls": result["model_calls"], "findings": [f.model_dump(mode="json") for f in result["findings"]],
              "brief": result["brief"].model_dump(mode="json"), "decision": result["decision"].model_dump(mode="json")}
    (args.output/"review.json").write_text(json.dumps(report, indent=2)+"\n")
    # review.json is the latest export; reviews.db retains separate case rows.
    print("Saved:", args.output/"review.json")
    if not args.demo and os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
        from langchain_core.tracers.langchain import wait_for_all_tracers
        from langsmith import Client
        wait_for_all_tracers()
        try:
            tracing_client = Client()
            project = tracing_client.read_project(project_name=os.environ.get("LANGSMITH_PROJECT", "accessscope"))
            async def verify_trace():
                traced_run = await tracing_client.runs.retrieve(str(run_id), project_id=str(project.id), selects=["ID", "TRACE_ID"])
                link = await tracing_client.runs.get_url(str(run_id), project_id=str(project.id), trace_id=str(traced_run.trace_id))
                return link.url
            trace_url = asyncio.run(verify_trace())
        except Exception:
            print("LangSmith trace delivery could not be verified; review results were saved.")
        else:
            investigation.update(trace_verified=True, trace_url=trace_url)
            report.update(trace_verified=True, trace_url=trace_url)
            (args.output/"investigation.json").write_text(json.dumps(investigation, indent=2)+"\n")
            (args.output/"review.json").write_text(json.dumps(report, indent=2)+"\n")
            print("LangSmith trace verified:", trace_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Third-party API errors can echo credential/request details. Expose only
        # their type/status here; local structured tool failures stay in the report.
        status = getattr(exc, "status_code", None)
        print(f"AccessScope stopped: {type(exc).__name__}" + (f" (HTTP {status})" if status else ""))
        raise SystemExit(1) from None
