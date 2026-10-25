# Read the real AccessScope agent

Keep this page beside the code. Begin with one case and follow what changes in state. The original scripted lesson is preserved separately in [DEMO_WALKTHROUGH.md](DEMO_WALKTHROUGH.md); the normal CLI now uses a real model and a conditional tool loop.

## 1. Start with the case, not the framework

A workload uses a role. A role has policy documents. A reviewer needs to understand that access and the limits of the evidence before making a decision.

The local default, `private/authsec-snapshot.json`, is a redacted database-derived case. It is not in public Git. It represents selected records from a source scan, not a fresh cloud scan. The included public fixture remains a fictional teaching input.

Open your snapshot locally. Find `workloads`, each `role_arn`, the corresponding role in `roles`, its policies and the coverage fields. These are the connections the agent will investigate. Do not guess owners, business purpose or effective access from names.

## 2. Give each kind of information its own contract

Open [models.py](accessscope/models.py).

`Snapshot` describes the input; `Finding` is one deterministic check outcome; `Brief` is an explanation; `Decision` is a review choice. These must not collapse into one model-generated paragraph. A paragraph cannot replace collected facts or authenticate a reviewer.

Pydantic parses/checks these records. `TypedDict` later names the graph's working fields. A snapshot fingerprint identifies exact stored contents; it is not a signature or a fresh AWS read.

Open [import_iga.py](accessscope/import_iga.py) if your input came from the database. It maps selected exported records into that same contract, pseudonymizes identifiers, redacts resource/condition strings and creates provenance before model use. It does not infer that a recorded owner accepted responsibility. The private salt enables stable pseudonyms locally without publishing the original identifiers.

## 3. Derive the checks before asking the model

Open [rules.py](accessscope/rules.py), starting at `evaluate`.

For every selected workload, ordinary code evaluates accepted production ownership, a recorded review deadline, and the exact configured wildcard-action Allow pattern. Each returns `finding`, `satisfied`, `unknown` or `not_applicable`.

Unknown is a result, not an exception to erase. Complete IAM inventory does not establish complete business context. A satisfied narrow wildcard search does not establish least privilege. In a database case with no verified owner-acceptance or purpose coverage, the context-dependent checks remain unknown.

Follow `evidence_catalog`: an outcome's evidence IDs connect it to actual policy/context records. Follow `shared_role_consumers`: changing one role can affect several selected programs. These helpers are reused by the agent's tools.

## 4. Read the capabilities before the model loop

Open [agent.py](accessscope/agent.py), from the argument contracts through `TOOLS`.

The model sees tool descriptions and JSON argument schemas. Python owns the implementations. A proposed tool name or role ARN is not permission to execute it.

| Capability | Need | Enforcement |
| --- | --- | --- |
| `get_review_findings` | Understand the case and evidence gaps | Returns this snapshot's evaluated outcomes |
| `get_evidence` | Inspect a cited source | ID must belong to the selected catalog |
| `get_role_evidence` | Avoid one model round trip per policy | Role must be selected; combined result stays bounded |
| `get_shared_consumers` | Check known shared impact | Returns only selected workload bindings |
| `search_aws_docs` | Research current provider guidance | Fixed public query topics, AWS-domain result filtering, two-search budget |
| `submit_review` | Finish with a consumable brief | Shape/reference checks, read-before-cite and known web-source IDs |

Tavily gets generic public queries defined in `DOC_QUERIES`. The model cannot make it search for a private account, policy body or arbitrary generated string. Research snippets remain untrusted data and cannot overrule account evidence.

Read `INSTRUCTIONS`. It tells the agent what investigation to perform, how to preserve uncertainty and how to finish. A prompt is guidance. Scope and budgets are enforced in code even if the model ignores the prompt.

## 5. Build the loop in your head

`AgentState` extends the common state with a conversation, pending tool calls, a tool log, read evidence IDs, web sources, a completion flag and usage counters. Credentials/SDK clients stay outside state in the constructed graph's dependencies.

Inside `build_agent_graph`, read `prepare`, `agent`, `tools`, then `review`.

```text
prepare
  -> agent chooses a tool
  -> tools validates arguments and executes its allowed operation
       -> agent sees the result and chooses again
       -> ...
       -> valid submit_review
  -> review pauses for external human input
  -> review resumes and validates the decision
  -> END
```

`prepare` validates the snapshot, derives outcomes and initializes counters. It does not call a model.

`agent` sends the conversation and tool schemas to GPT-4o. A response requests one function call. The code preserves that output as primitive JSON and records actual returned token usage. It limits model turns, per-request output, timeout and context size.

`tools` parses the selected function's arguments with its Pydantic contract. The executor checks snapshot scope, then returns a `function_call_output` carrying the matching call ID. That call ID connects the model's request to our result. A tool failure is returned as a bounded error so the model can correct the request within its remaining budget.

The conditional edge routes back to `agent` until `submit_review` passes validation. A model saying "finished" in prose is not enough to set `finished=True`.

`StateGraph` is the builder: register functions and routes, then `compile` creates an executable graph. The returned object runs when the CLI calls `stream`/`invoke`. No custom reducers are used; each node returns updated values, preserving fields it omitted. Conversation/history updates therefore explicitly include the preceding list.

**Check yourself:** point to the actual edge that repeats the model/tool cycle. Then point to the executor branch that rejects an out-of-scope role. Those are different responsibilities.

## 6. Separate an agent submission from a human decision

The final `Submission` contains a `Brief` and web-source IDs. The validator ensures references exist, active findings are covered, cited evidence was read and cited web IDs were returned by research. It does not prove every sentence is supported merely because IDs match.

The `review` node interrupts only after this investigation has finished. Its checkpoint/thread ID preserves where execution paused. On resume, that node restarts and `interrupt()` returns the external choice; model investigation is not repeated.

The decision fingerprint must match the stored snapshot. It does not authenticate the reviewer or verify that AWS stayed unchanged afterward. There is no permission-change executor.

Open [workflow.py](accessscope/workflow.py), `record_decision`. SQLite stores completed decisions with `(case_id, fingerprint)` as the key. An identical retry retains one row; conflicting input is rejected. This audit survives on disk. The current in-memory graph checkpoint does not survive process restart.

The fixed `build_graph`/`DemoExplainer` lesson and older one-call explainer are available in that file for learning/backward compatibility. The normal CLI constructs `build_agent_graph` from `agent.py`.

## 7. Connect the entrypoint and the output files

Open [cli.py](accessscope/cli.py) alongside [settings.py](accessscope/settings.py).

```text
arguments and local settings
  -> read selected snapshot (or explicit AWS collection)
  -> save input
  -> construct real agent graph
  -> stream tool progress until review pause
  -> save investigation.json
  -> collect a review choice
  -> resume same thread
  -> save audit and review.json
```

The local `.env` is ignored; exported variables take precedence. `CHAT_MODEL=gpt-4o` selects the requested model, with `OPENAI_MODEL` as an optional override. A missing private input or required credential is an error; it does not select dummy output.

`--demo` explicitly selects the old offline lesson and does not load `.env`. The separate teaching command `python -m accessscope.walkthrough` remains offline too.

`investigation.json` is saved before the human prompt and includes chosen tools, results, web sources, model-call count, usage and trace run ID. `review.json` adds the completed choice; `reviews.db` retains separate case rows. `--decision investigate` supplies an external choice automatically while still exercising the real interrupt/resume.

With tracing enabled, selected evidence/tool results go to LangSmith. Do not treat "tracing" as harmless local logging. Use an authorized payload and understand the destination. API keys themselves are not graph state or model input.

## 8. Swap data sources without rewriting the investigator

Open [collect.py](accessscope/collect.py). It performs scoped read-only AWS collection and returns the same `Snapshot` type. The database importer and direct collector are input adapters; the rules and agent operate on their common output.

For the database case, inspect the private provenance file for source scan time and missing context. For direct collection later, use explicit AWS profile/region and exact workload ARNs. Do not turn a denied scan, unsupported construct or stale snapshot into a clean conclusion.

## 9. Challenge the boundaries

Run `uv run pytest -q`. [test_review.py](tests/test_review.py) covers the original contracts, rules, pause/resume, audit and collection boundaries. [test_agent.py](tests/test_agent.py) exercises the actual conditional agent graph with fake API boundaries: chosen tools, rejected scope, read-before-cite, turn budget, research filtering and no repeat calls on human resume.

Fakes are appropriate for deterministic regression checks. They do not substitute for a live integration run and cannot measure real-model factuality, helpfulness or cost. Inspect a live report to establish what actually happened on that run.

## 10. Explain a real run without exaggeration

Use the report, not a memorized success story. Say which snapshot was used, when its source scan happened, which checks were unknown, which tools the model chose, what sources it consulted and what the reviewer decided.

A strong architecture answer follows **need -> mechanism -> limitation -> improvement**:

- Many policies create repeated round trips -> batch selected role evidence -> keep the result bounded -> measure tokens/latency and missing evidence before changing budgets.
- A review must name its input -> snapshot-bound decision -> no live-state recheck -> revalidate before any future write.
- A model can cite real IDs with false prose -> mechanical validation plus human review -> no semantic guarantee -> evaluate labelled cases against a template baseline.
- A hosted product needs restart recovery -> in-memory saver today -> unfinished state is lost -> use durable checkpoints and authorize reviewer access.

Finally, draw the loop from memory. Explain who chooses a tool, who enforces scope, why one role can affect two workloads, why unknown is preserved, and what happens when the human resumes. That is the intuition you need to defend the implementation.
