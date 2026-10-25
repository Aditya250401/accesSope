"""Watch the real demo graph build its state: uv run python -m accessscope.walkthrough.

This learning runner always uses fictional fixture data and DemoExplainer. It makes
no AWS/model calls and keeps its example audit database in a temporary directory.
Read this file LAST: it observes the same core modules rather than replacing them.
"""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from langgraph.types import Command

from .models import Snapshot
from .workflow import build_graph, record_decision


def show_updates(graph, graph_input, config):
    # stream(..., stream_mode="updates") executes the graph just as invoke() does,
    # but yields node updates as they occur. This is visibility, not a new pipeline.
    for event in graph.stream(graph_input, config, stream_mode="updates"):
        for node_name, update in event.items():
            if node_name == "__interrupt__":
                print("  PAUSED: review needs a decision; the graph has not reached END.")
                continue
            print(f"  {node_name} returned keys: {', '.join(update)}")
            if node_name == "check_configuration":
                for finding in update["findings"]:
                    print(f"    {finding.id:30} {finding.outcome:10} {finding.evidence_ids}")
            elif node_name == "explain":
                print("    live model calls:", update["model_calls"])
                print("    brief:", update["brief"].summary)
            elif node_name == "validate":
                print("    references/coverage accepted; prose truth still needs review")
            elif node_name == "review":
                print("    decision:", update["decision"].action)


def main():
    fixture = Path(__file__).resolve().parents[1] / "fixtures/demo.json"
    snapshot = Snapshot.model_validate_json(fixture.read_text())
    graph = build_graph()  # No argument selects the scripted DemoExplainer.
    config = {"configurable": {"thread_id": "walkthrough-demo"}}

    print("1. Load the fictional snapshot")
    print("  workloads:", ", ".join(w.id for w in snapshot.workloads))
    print("  snapshot as_of:", snapshot.as_of, "(not today's clock)")
    print("\n2. First execution: watch state arrive one stage at a time")
    show_updates(graph, {"snapshot": snapshot}, config)

    # get_state() inspects this thread's checkpoint; it does not run another node.
    paused = graph.get_state(config)
    print("\n3. Inspect the paused execution")
    print("  available state keys:", ", ".join(sorted(paused.values)))
    print("  decision exists:", "decision" in paused.values)
    print("  next node:", paused.next)
    print("  fingerprint prefix:", paused.values["fingerprint"][:12], "(display only)")

    # We deliberately supply a choice in this teaching run. A hosted application
    # would authenticate/authorize its reviewer before allowing this resume call.
    decision_input = {
        "action": "investigate",
        "reviewer": "walkthrough-learner",
        "reason": "Inspect missing ownership, overdue review and shared-role impact.",
        "snapshot_fingerprint": paused.values["fingerprint"],
    }
    print("\n4. Resume the SAME graph/thread with an investigate decision")
    show_updates(graph, Command(resume=decision_input), config)
    finished = graph.get_state(config)
    print("  next nodes after completion:", finished.next)
    print("  live model calls:", finished.values["model_calls"])

    print("\n5. Record exactly the same decision twice")
    with TemporaryDirectory(prefix="accessscope-learning-") as directory:
        path = Path(directory) / "reviews.db"
        record_decision(path, "walkthrough-demo", finished.values)
        record_decision(path, "walkthrough-demo", finished.values)
        with sqlite3.connect(path) as db:
            count = db.execute("SELECT count(*) FROM decisions").fetchone()[0]
        print("  audit rows:", count, "(an identical retry did not add a second row)")
    print("  The temporary learning database is now removed.")
    print("\nUse accessscope.cli for persistent outputs/latest/{snapshot.json,review.json,reviews.db}.")
    print("Final choice:", json.dumps(finished.values["decision"].model_dump(mode="json")))


if __name__ == "__main__":
    main()
