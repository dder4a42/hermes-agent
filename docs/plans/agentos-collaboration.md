# Migrating an AgentOS-style Collaboration Mechanism into Hermes

Source of truth for the target design: **Apodex 1.1: Scaling Agentic Intelligence
for Complex Work** (arXiv 2608.23283v2, 2026-08-24/25), §3.2 (Agent Team) and
§3.3 (AgentOS). Cached full text for this machine:
`~/.hermes/cache/web/arxiv.org-7a7f9e28ea.md`.

This document maps that design onto Hermes' existing long-horizon work
(`agent/longtask_board.py`, `tools/longtask_tool.py`,
`tools/delegate_tool.py`, `agents/longtask_verifier.py`,
`plugins/context_engine/long_horizon/`) and phases the port.

## 1. What we already have vs what the target adds

Already present:

- a durable board keyed by session (`longtask_board.py`) with DAG validation,
  atomic writes, and a board lock;
- `delegate_task` with isolated children, `output_schema` validation, a
  background/fan-out rail, and a claim-evidence report convention;
- a deterministic + optional LLM verifier (`longtask_verifier.py`);
- a context engine that carries the board into a compression handoff.

Missing, and named by the paper:

| Target property (paper) | Hermes today |
|---|---|
| Board is runtime-managed coordination state (K_t), not a model artefact | Board is written by the model only |
| Item fields: owner set, dependency refs, **refs to returned evidence/artifacts**, resolution ∈ {open, in_progress, resolved, cancelled} | `status` (8 values) + claims/evidence; no evidence/artifact refs, no owner set |
| **Execution status is runtime-owned; resolution is coordinator-owned** ("prevents a Task Board update from overwriting the state of a live asynchronous job") | One `status` field conflates them; `attach_report` advances it from the child's self-report |
| A subagent execution may be **reported while its item stays open** (pending verification) | `attach_report(status="success")` → `done` (we even aliased it) |
| Verifier is **asymmetric**: it receives one claim + its evidence + the applicable constraint and attacks it | Verifier receives a whole report, and the report is the parent's transcription of the child's summary |
| Evidence/artifact references (I_t) + provenance graph G_t (source → action → artifact) | Nothing persisted: 5 id spaces with no join key |
| Finalization gate: no final answer until every active item is resolved/cancelled; planning mode also requires ≥1 delegated branch and an independent verifier | No gate at all |
| Board re-injected **periodically** so the plan survives compaction | Rendered only inside a compression handoff |
| Tiered compaction: evict old tool bodies first (keep structure, recent results, **protected fan-in reports**), escalate to LLM only if the measured relief is insufficient | Last 10 messages kept verbatim; no eviction, no protected fan-in set |

## 2. What we deliberately do NOT copy

- **The trained policy.** Apodex trains decomposition/delegation/integration/
  replanning into the model. We run third-party models, so coordination
  behaviour cannot be assumed — it must be **host-enforced** (structured
  contracts, gate, provenance). This inverts the paper's emphasis, not its
  architecture.
- **Physical substrate details** (worker pools, sticky routing, sandbox
  reclaim, provider-side token accounting). We already have terminal backends
  and a session DB; we adopt the *contracts*, not the machinery.
- **Single-writer publication lease** (`/outputs` manifest + baseline
  reconciliation). Valuable, but it is a delivery-layer concern; keep it as a
  later phase, not a prerequisite for coordination.

Invariants we must not break while porting (AGENTS.md hardlines):

- prompt caching: no mid-conversation system-prompt/toolset rebuild, and **no
  synthetic user message injected mid-loop** — board re-injection rides the
  steering channel (inside an existing tool result), the pattern already used
  for `STEER_CHANNEL_NOTE`;
- strict message role alternation;
- no new core tool unless terminal + file cannot do the job — this port extends
  existing board tools and adds **one optional schema field** (`node_id`) to
  `delegate_task`.

## 3. Phases

Each phase is independently shippable, testable, and leaves the tree green.
Order matters: P1 fixes the data model, P0 makes the board mutable (which the
paper requires — "during execution the board remains mutable"), then the host
takes over the loop, then verification narrows, then the gates, with the
compression work in parallel.

### P1 — Separate execution status from resolution

- Node fields: `execution` ∈ {none, queued, running, reported, failed, timeout,
  cancelled}, owned by the host; `resolution` ∈ {open, in_progress, resolved,
  cancelled}, owned by the agent.
- `attach_report` records the report and sets `execution="reported"`; it **must
  not** advance `resolution`. This reverts the `success → done` alias mapping:
  a child's self-report is evidence, not a verdict.
- `resolved` requires a verifier verdict (config gate
  `longtask.require_verification_before_unlock`, already present, extended to
  the new field).
- Dependents unlock on `resolution == "resolved"`, not on a report arriving.
- Tests: attach leaves the item open; downstream stays blocked until resolved;
  execution status never overrides a live item.

### P0 — Mutable board (replanning surface)

- New tool `longtask_add_node` (+ `longtask_cancel_node`): add nodes with
  dependencies on existing items, cancel obsolete items; cycle + unknown-dep
  validation reuses the existing DAG checks.
- Rationale from the paper: intervention and replanning are expressed as
  tool-mediated edits to the board; a create-only DAG cannot express "follow-up
  work appeared".
- Tests: add → dependency order respected; cancel → dependents behave; cycles
  rejected.

### P2 — Host-mediated report return + provenance

- `delegate_task` task items gain an optional `node_id`. The parent passes it at
  dispatch, so the host knows the item before the child runs.
- `_finalize_child_results` (right beside the existing `on_delegation` call):
  when `node_id` is present and the child produced a schema-valid report, the
  host attaches it to that item and records provenance —
  `child_session_id`, `task_index`, `delegation_id`, archive `report_id`,
  `execution="reported"`.
- Result entries and the `on_delegation` kwargs gain those ids, so the board and
  `delegation_reports.json` finally share a key. The parent no longer transcribes.
- Tests: E2E with a temp `HERMES_HOME` — dispatch with `node_id`, assert the
  board gains the report + provenance with no `longtask_attach_report` call, and
  that the archive entry carries the same ids.

### P3 — Asymmetric, per-claim verification

- Verifier input: one claim + its evidence + the node goal/constraint (not the
  whole report). Output: `{contested, disconfirming_evidence, required_repair}`
  plus the verdict, stored per claim on the item.
- `resolved` requires every load-bearing claim to be non-contested.
- Deterministic evidence checks gain **content-level** verification:
  `ref="path:line"` must have its `quote` actually present in that file, and a
  bare path is recorded as weak evidence. This closes the hole found by
  dogfooding (file-existence cannot support a claim about content).
- Tests: an unsupported claim is contested; a supported one passes; a quote that
  is absent from the cited file fails.

### P4 — Finalization gate + board re-injection

- Gate: with unresolved items, a terminal answer is refused or flagged
  (`longtask.enforce_finalization_gate`, default warn-first). Planning mode may
  additionally require at least one delegated branch and a verifier run.
- **Re-injection is event-driven, not clock-driven**, and rides tool results the
  model already receives (the steering channel): the payloads of `longtask_next`
  / `longtask_read` / `longtask_verify_node` and the fan-in completion of
  `delegate_task`. Never a synthetic mid-loop user message; never a rewrite of an
  earlier message.
  - Primary mechanism: the board state comes back attached to calls the model is
    already making about the board, so the marginal cost is near zero.
  - Safety net only: if the model has not consulted the board for K turns
    (`longtask.board_reinject_idle_turns`, default ~5) while items remain
    unresolved, emit one budgeted render.
  - Suppression: if the render's content hash is unchanged since the last one,
    emit nothing (or a one-line "board unchanged").
- **Cache reasoning (why not "periodically").** Appending a render preserves the
  cached prefix, but the render then lives in history and is re-sent on every
  later call until compression, so clock-driven injection costs tokens
  quadratically over a long run. Rewriting an older render to keep "only the
  latest board" would mutate past context and break the prefix cache — forbidden
  (AGENTS.md: compression is the one sanctioned exception). The paper's stated
  purpose for re-injection is "so the plan remains visible **after context
  compaction**", which the compression handoff already satisfies.
- **One board per run.** The paper's board is run-scoped ("a run-scoped task
  board") and parallel workstreams are expressed with the per-item *group*
  field. Multiple boards — or per-agent shadow boards — are an anti-pattern we
  have already been bitten by (children resolving their own session directory).
  A materially changed objective starts a new task contract/run (new board)
  while carrying forward valid workspace state; that boundary is not yet
  implemented.
- **Renders are snapshots, never state.** The board file is the single source of
  truth. Every render carries a sequence number and explicit "snapshot" wording,
  so older renders in history read as history; compression collapses all of them
  into the single handoff render, so the post-compression context holds exactly
  one board render.
- Tests: gate blocks on open items and passes when clean; injection fires on
  events and is suppressed when nothing changed; no role-alternation or
  mid-loop-user-message regressions; post-compression context holds exactly one
  board render.

### P5 — Compression tiering (parallel, independent)

- Evict bodies of older tool observations first, keeping message structure,
  recent results, and **fan-in reports** (subagent reports are the paper's
  protected set); only then build the LLM handoff.
- Trigger stays provider-reported token usage; escalation is driven by the
  relief actually obtained.
- Tests: a ~200KB tool-body tail compresses below threshold; a fan-in report
  survives eviction verbatim.

### P6 — (Optional, later) Artifact index + delivery reconciliation

- Minimal `I_t`: path + hash + producing node/child, plus a deliverable manifest
  that a verifier can distinguish from intermediate scratch.

## 4. Open decisions

- Should the finalization gate ever hard-fail by default, or warn first until the
  flow has mileage? (Recommendation: warn first, hard-fail behind config.)
- Do we keep a model-facing `longtask_attach_report` once the host attaches
  automatically? (Recommendation: keep — manual reports and schema-less
  delegations still need it.)
- Is `execution` runtime-owned enough in-process, or must it survive a restart?
  (Recommendation: in-process first; the board lock already serializes writes.)

## 5. Evidence trail

- `docs/plans/long-horizon-task.md` records the current wiring and follow-ups.
- `.hermes/tasks/<session>/reports/*.json` holds the audited inventory of
  persistence paths (N1) and the tail-budget assessment (N2), both with
  file:line evidence, produced by the pre-port flow.

## 6. Incident log

**2026-09-12 — compression abandoned as "no progress" (stopgap f11240ca28; config fixed; P7/P8).**

Two halves, and the engine was never one of them:

1. **The route.** `memory.session_archive.llm_summary` pinned the summariser to
   `provider: Models.sjtu.edu.cn` / `model: qwen` — the provider value is a
   *display name*, not a provider id. In a clean process that resolution ends in
   `AuthenticationError: LiteLLM Virtual Key expected. Received=no-k…ired` (401)
   in 1.2s; inside the live agent it silently rode the main runtime while
   swapping the model to `qwen`, so nothing surfaced. Every chunk then stalled
   into the 30s timeout (plus the auxiliary client's transient retry).
2. **Invisibility.** Nothing in that stretch ticked the pass's progress fence, so
   the host's inactivity watchdog (`compression.context_timeout_seconds`, default
   120s) fired — ten `session_archive_summary` calls between 03:49:52 and
   03:51:51, then "continuing without compression". The context stayed ~638k
   tokens and the next request died on the provider's token limit (HTTP 429).

The long_horizon handoff is local and fast; the earlier 02:56 pass committed a
179-message session in 65.6s. Compression was not the failing part — a silent,
slow, mistargeted auxiliary call inside it was.

**Decision (user, 2026-09-12): in-path summarisation is the design, not the bug.**
Triggering compression IS a context rebuild, and the rebuilt context is what the
following turns reason from — so summary quality outranks pass latency. What
therefore had to change:

- **Summariser = the main model.** `provider: auto` / `model: auto` (measured
  4.8s in `call_llm`, 7.7s through the archive's own `_summarize_chunk`, real LLM
  output, served by the main route). Context and capability now track the main
  agent; `max_input_chars` raised 12k → 24k so tool-heavy chunks are not clipped.
- **The pass must be able to see the progress.** Chunk summaries run concurrently
  (P7, measured: 12 chunks / 12 calls / 12 LLM summaries in 11.6s at
  `llm_summary_concurrency: 4`, per-call 1.5-7.9s ⇒ ~35s extrapolated for the
  36-chunk / 700-message case, versus ~139s serially). Heartbeating the fence is
  the remaining piece for backlogs that outrun even that.
- **Budgets become an abort valve, not the normal path** — `max_llm_summaries_per_pass`
  (64) / `llm_summary_budget_seconds` (120s) exist to stop a hung provider, and
  they sit just under the host's own inactivity window so the archive degrades to
  deterministic recaps on its own terms instead of being killed mid-pass.

Lesson for this port: anything a plugin runs inside `on_pre_compress` sits on the
compression critical path and is **invisible to the host's progress tracker unless
it ticks the fence**. Chunks (the recoverable evidence) must be written
unconditionally; enrichment may be rationed only as an abort valve. And an
unrecognized `provider` in auxiliary config must fail loudly rather than silently
inheriting the main runtime with a substituted model (P8).


