"""Run a reproducible fixture review, or explicitly collect/explain with live services."""
import argparse
import json
import os
from pathlib import Path
import uuid

from langgraph.types import Command

from .models import Snapshot
from .workflow import OpenAIExplainer, build_graph, record_decision


def main():
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
        session = boto3.Session(region_name=args.region)
        account = session.client("sts").get_caller_identity()["Account"]
        context = json.loads(args.contexts.read_text())
        snapshot = collect(session.client("iam"), session.client("lambda"), account_id=account,
                           region=args.region, contexts=context["workloads"], context_complete=context["complete"])
    else:
        snapshot = Snapshot.model_validate_json(args.snapshot.read_text())
    snapshot_path = args.output/"snapshot.json"
    snapshot_path.write_text(snapshot.model_dump_json(indent=2)+"\n")
    if args.live_model and not os.environ.get("OPENAI_MODEL"):
        parser.error("Set OPENAI_MODEL to a structured-output model available to your account")
    explainer = OpenAIExplainer(os.environ["OPENAI_MODEL"]) if args.live_model else None
    graph = build_graph(explainer)
    case_id = args.case_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": case_id}}
    result = graph.invoke({"snapshot": snapshot}, config)
    print("Mode:", "LIVE MODEL" if args.live_model else "SCRIPTED DEMO (no model call)")
    print("AWS writes: none; effective access: not evaluated")
    print(result["brief"].summary)
    for finding in result["findings"]:
        print(f"  {finding.outcome:14} {finding.id}: {finding.explanation}")
    if result.get("__interrupt__"):
        action = args.decision or input("Review choice [retain/investigate/propose_change]: ").strip()
        result = graph.invoke(Command(resume={"action": action, "reviewer": args.reviewer,
                                             "reason": args.reason, "snapshot_fingerprint": snapshot.fingerprint()}), config)
    record_decision(args.output/"reviews.db", case_id, result)
    report = {"mode": "live_model" if args.live_model else "scripted_demo", "aws_writes": False,
              "case_id": case_id, "effective_access": "not_evaluated", "snapshot_fingerprint": result["fingerprint"],
              "model_calls": result["model_calls"], "findings": [f.model_dump(mode="json") for f in result["findings"]],
              "brief": result["brief"].model_dump(mode="json"), "decision": result["decision"].model_dump(mode="json")}
    (args.output/"review.json").write_text(json.dumps(report, indent=2)+"\n")
    print("Saved:", args.output/"review.json")


if __name__ == "__main__":
    main()
