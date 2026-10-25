"""Reading stop 7 in WALKTHROUGH.md: connect input, graph execution and saved files.

uv run python -m accessscope.cli --decision investigate
The default needs no API keys: fictional input + scripted explanation + real graph.
"""
import argparse
import json
import os
from pathlib import Path
import uuid

from langgraph.types import Command

from .models import Snapshot
from .workflow import OpenAIExplainer, build_graph, record_decision


def main():
    # Arguments select the input and explainer independently. Reading real AWS
    # does not automatically enable the model; enabling a model does not select AWS.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path(__file__).resolve().parents[1]/"fixtures/demo.json")
    parser.add_argument("--output", type=Path, default=Path("outputs/latest"))
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--decision", choices=["retain", "investigate", "propose_change"])
    parser.add_argument("--reviewer", default="local-demo-reviewer")
    parser.add_argument("--reason", default="Inspect the evidence before proposing any source change.")
    parser.add_argument("--case-id", help="Reuse an explicit ID to retry the same review; a new run otherwise starts a new case")
    parser.add_argument("--collect-aws", action="store_true")
    parser.add_argument("--contexts", type=Path)
    parser.add_argument("--region")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.collect_aws:
        if not args.contexts or not args.region:
            parser.error("AWS collection requires --contexts and --region")
        import boto3
        from .collect import collect
        # boto3 resolves local AWS credentials. The collector reads configuration;
        # the supplied contexts add ownership/purpose AWS cannot establish for us.
        session = boto3.Session(region_name=args.region)
        account = session.client("sts").get_caller_identity()["Account"]
        context = json.loads(args.contexts.read_text())
        snapshot = collect(session.client("iam"), session.client("lambda"), account_id=account,
                           region=args.region, contexts=context["workloads"], context_complete=context["complete"])
    else:
        # Parse JSON into the same Snapshot contract that live collection returns.
        # The downstream rules/graph do not need to know which adapter supplied it.
        snapshot = Snapshot.model_validate_json(args.snapshot.read_text())
    snapshot_path = args.output/"snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2)+"\n")
    # Saving input lets a reviewer inspect what this run actually used.
    if args.live_model and not os.environ.get("OPENAI_MODEL"):
        parser.error("Set OPENAI_MODEL to a structured-output model available to your account")
    explainer = OpenAIExplainer(os.environ["OPENAI_MODEL"]) if args.live_model else None
    # build_graph(None) selects DemoExplainer. Constructing the graph does not run it.
    graph = build_graph(explainer)
    case_id = args.case_id or str(uuid.uuid4())
    # case_id names this review; thread_id tells the checkpointer which execution
    # to resume. They share a value here, but neither authenticates the reviewer.
    config = {"configurable": {"thread_id": case_id}}
    result = graph.invoke({"snapshot": snapshot}, config)
    # This FIRST invocation runs checks -> explain -> validate -> review, then
    # returns at the interrupt. No decision exists yet, even with --decision set.
    print("Mode:", "LIVE MODEL" if args.live_model else "SCRIPTED DEMO (no model call)")
    print("AWS writes: none; effective access: not evaluated")
    print(result["brief"].summary)
    for finding in result["findings"]:
        print(f"  {finding.outcome:14} {finding.id}: {finding.explanation}")
    if result.get("__interrupt__"):
        # input() is the CLI waiting for a person. interrupt() was the GRAPH pause.
        # --decision is a convenient way to supply the choice without typing it.
        action = args.decision or input("Review choice [retain/investigate/propose_change]: ").strip()
        result = graph.invoke(Command(resume={"action": action, "reviewer": args.reviewer,
                                             "reason": args.reason, "snapshot_fingerprint": snapshot.fingerprint()}), config)
        # Same graph + same thread -> restart review; resume data is returned by
        # its interrupt() call. Earlier explanation work is not repeated here.
    # Completion, audit storage and report export are separate responsibilities.
    record_decision(args.output/"reviews.db", case_id, result)
    report = {"mode": "live_model" if args.live_model else "scripted_demo", "aws_writes": False,
              "case_id": case_id, "effective_access": "not_evaluated", "snapshot_fingerprint": result["fingerprint"],
              "model_calls": result["model_calls"], "findings": [f.model_dump(mode="json") for f in result["findings"]],
              "brief": result["brief"].model_dump(mode="json"), "decision": result["decision"].model_dump(mode="json")}
    (args.output/"review.json").write_text(json.dumps(report, indent=2)+"\n")
    # review.json is the latest export; reviews.db retains separate case rows.
    print("Saved:", args.output/"review.json")


if __name__ == "__main__":
    main()
