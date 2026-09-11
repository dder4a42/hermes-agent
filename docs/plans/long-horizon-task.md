# Long-Horizon Task Orchestration Plan

## Goal

Build a long-horizon task mode where the main agent keeps the global objective,
reasoning state, and scheduling state stable while subagents execute bounded
nodes of work. Subagents report back in a claim-evidence format, a verifier
checks the report asymmetrically, and context compression preserves recoverable
history instead of permanently discarding important tool evidence.

This plan is designed for the `dev/long-horizon-task` branch.

## Current Hermes Fit

Hermes already has useful foundations:

- `delegate_task` spawns isolated child agents and returns only the final child
  summary to the parent context.
- Subagent routing is configurable via `delegation.provider`,
  `delegation.model`, `delegation.base_url`, and related runtime-provider
  resolution.
- Memory providers already expose `on_pre_compress()`, `on_delegation()`,
  `get_tool_schemas()`, and `handle_tool_call()`.
- Context-engine plugins can replace or augment compression through
  `ContextEngine.compress()` and can expose retrieval tools.

The missing pieces are a durable task graph, structured reports, verifier
handoff, and a compression checkpoint/archive layer.

## Phase 1: Skill And Task Board

Add a long-horizon skill as the user-facing entry protocol.

Proposed files:

- `skills/long-horizon-task/SKILL.md`
- `agent/longtask_board.py`
- `tests/agent/test_longtask_board.py`

The skill should instruct the main agent to:

- classify whether a request is simple or complex;
- for complex requests, create a task graph before executing;
- maintain global objective, assumptions, constraints, decisions, open
  questions, and next-ready nodes in the task board;
- delegate only ready nodes whose dependencies are satisfied;
- require subagent reports in claim-evidence form;
- update the board after each subagent report and verification pass.

Initial board storage should be simple and inspectable:

- `.hermes/tasks/<session_id>/board.json`
- `.hermes/tasks/<session_id>/reports/<node_id>.json`
- `.hermes/tasks/<session_id>/evidence/`

Draft schema:

```json
{
  "task_id": "task-...",
  "objective": "...",
  "nodes": [
    {
      "node_id": "N1",
      "goal": "...",
      "dependencies": [],
      "status": "pending",
      "assigned_to": null,
      "claims": [],
      "evidence": [],
      "report_path": null,
      "verification": null
    }
  ],
  "global_state": {
    "decisions": [],
    "constraints": [],
    "open_questions": [],
    "next_ready": []
  }
}
```

## Phase 2: Longtask Toolset

Add a non-core toolset so state transitions are validated by host code instead
of relying on the model to edit the board perfectly.

Proposed file:

- `tools/longtask_tool.py`

Initial tools:

- `longtask_create(objective, nodes)`
- `longtask_read(node_id?)`
- `longtask_next()`
- `longtask_update_node(node_id, status, ...)`
- `longtask_attach_report(node_id, report)`
- `longtask_verify_node(node_id, report?)`

Host-side guarantees:

- validate node IDs and dependency references;
- compute ready nodes by dependency/topological order;
- reject invalid status transitions;
- write board updates atomically;
- keep tool results short while writing full reports to disk.

The intended loop is:

1. main agent creates or reads the board;
2. main agent calls `longtask_next()`;
3. ready nodes are delegated with `delegate_task`;
4. child reports are attached to the board;
5. verifier results unlock downstream nodes.

## Phase 3: Structured Delegation Report And Verifier

Extend subagent prompts and finalization so every child produces a durable
report, including partial failure and timeout cases.

Relevant existing file:

- `tools/delegate_tool.py`

Report schema:

```json
{
  "status": "success|partial|failed|timeout",
  "claims": [
    {
      "claim": "...",
      "evidence": [
        {
          "kind": "file|command|url|observation",
          "ref": "...",
          "quote": "...",
          "confidence": 0.8
        }
      ],
      "confidence": 0.8
    }
  ],
  "changed_files": [],
  "open_questions": [],
  "recommended_next": []
}
```

Add a verifier module:

- `agent/longtask_verifier.py`
- `tests/agent/test_longtask_verifier.py`

Verifier output:

```json
{
  "verdict": "accepted|rejected|needs_followup",
  "accepted_claims": [],
  "rejected_claims": [],
  "missing_evidence": [],
  "summary_for_parent": "..."
}
```

Start with deterministic checks:

- referenced files exist;
- command evidence includes command, exit code, and output excerpt;
- report status matches evidence quality;
- accepted claims have at least one concrete evidence item.

LLM-based verification can be added later as an optional, configured pass.
The first implementation supports this optional pass through:

```yaml
longtask:
  verifier:
    enabled: true
    provider: Models.sjtu.edu.cn
    model: deepseek-reasoner
    timeout: 30
    max_tokens: 1200
```

When disabled or unavailable, Hermes keeps the deterministic verifier verdict
and records the LLM verifier failure in the node verification object.

## Phase 4: Session Archive Memory

Add an external memory provider that checkpoints context before compression.
This prevents compression from becoming a permanent information-destroying
operation.

Proposed plugin:

- `plugins/memory/session_archive/`

Use existing hooks:

- `on_pre_compress(messages, evidence_messages=...)`
- `on_delegation(task, result, child_session_id=...)`
- `get_tool_schemas()`
- `handle_tool_call()`

Expose tools:

- `session_archive_search(query, role?, tool_name?, time_range?)`
- `session_archive_expand(chunk_id)`
- `session_archive_get_claims(task_id?)`

Storage model:

- chunk original messages before compression;
- preserve raw tool outputs outside the prompt;
- attach message IDs, role, tool name, task node, timestamps, and hashes;
- generate rolling digests over chunks;
- return a compact checkpoint summary to compression as `memory_context`.

## Phase 5: Long-Horizon Context Engine

Add a context-engine plugin that uses the session archive and task board as the
authoritative long-horizon state.

Proposed plugin:

- `plugins/context_engine/long_horizon/`

Compression output should preserve:

- active user request and newest incomplete tool round;
- task board manifest;
- current objective, accepted decisions, constraints, and open questions;
- claim-evidence digest with archive chunk references;
- retrieval instructions for expanding archived chunks.

Compression output should avoid preserving:

- large old tool bodies in prompt;
- stale subagent intermediate logs;
- repeated summaries that are already represented in the board/archive;
- oversized protected tails that prevent compression from making progress.

## Next Compression Plan

The next compression work should be implemented after Phase 1 and Phase 2 prove
the task board loop works.

Step 1: pre-compress archive provider.

- Implement `plugins/memory/session_archive/` with `on_pre_compress()`.
- Persist raw pre-compression messages and normalized evidence chunks.
- Return a short manifest to the existing compressor through `memory_context`.
- Add tests that compression still succeeds when the provider archives data,
  and that archived chunks can be searched and expanded after compaction.

Step 2: retrieval tools.

- Add `session_archive_search()` and `session_archive_expand()`.
- Gate them through the memory-provider tool path, not core tools.
- Keep returned snippets bounded and include stable chunk IDs.
- Add tests for role/tool filters and expansion by chunk ID.

Step 3: compression manifest.

- Teach the long-horizon skill and later context engine to prefer board state
  and archive references over raw historical tool output.
- Keep claim-evidence summaries in prompt, but store full evidence in the
  archive.

Step 4: context-engine override.

- Implement `plugins/context_engine/long_horizon/`.
- Replace the default head/tail-heavy output with a compact handoff:
  active turn, task graph state, accepted claims, unresolved claims, and archive
  pointers.
- Verify that long sessions can compact even when recent user turns and tool
  outputs would otherwise exceed the protected tail budget.

Step 5: strict checkpoint mode.

- Evaluate enabling `compression_checkpoint_required` when
  `session_archive` is active.
- In strict mode, compression must fail closed if the archive checkpoint cannot
  be written, preventing irreversible loss of pre-compression context.

## SJTU Routing Notes

Current practical routing for this project:

- main model provider: `Models.sjtu.edu.cn`
- base URL: `https://models.sjtu.edu.cn/api/v1`
- `NO_PROXY` should include `models.sjtu.edu.cn` so Hermes bypasses mihomo for
  that provider and lets strongSwan/IPsec route it through the SJTU VPN.

Subagent model selection can be configured today:

```yaml
delegation:
  provider: Models.sjtu.edu.cn
  model: deepseek-reasoner
  max_concurrent_children: 3
  max_spawn_depth: 1
  max_summary_chars: 12000
  child_timeout_seconds: 0
```

Later, add task-type routing so small verifier/planner work can use a cheaper
SJTU Qwen model while multimodal nodes can use the SJTU MiniMax model.

## Suggested Commit Sequence

1. `feat(longtask): add board model and skill`
2. `feat(longtask): add task board toolset`
3. `feat(delegation): require structured subagent reports`
4. `feat(longtask): add verifier pass`
5. `feat(memory): add session archive provider`
6. `feat(context): add long horizon context engine`
