---
name: long-horizon-task
description: "Plan and verify multi-step work on a durable task board."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [planning, delegation, long-horizon, task-graph, verification]
    requires_toolsets: [longtask, delegation]
---

# Long-Horizon Task

Use this skill when the user's request is complex enough to require multiple
phases, multiple independent investigations, subagent delegation, or state that
must survive context compression.

## Prerequisites

This skill needs the `longtask` (board) and `delegation` (`delegate_task`)
toolsets. Both must be in the session's schema — `requires_toolsets` hides this
skill from the index otherwise, so if you can read this, the `longtask_*` tools
are available and the first step below is executable.

`longtask` is NOT part of the core toolset, because its six schemas would
otherwise ride on every API call for users who never plan this way. Enable it
per platform in `config.yaml`:

```yaml
platform_toolsets:
  cli: [hermes-cli, longtask]
```

`delegation` is on by default wherever the core toolset is enabled. Verify with
`longtask_read` / `longtask_next` before assuming a board exists — never invent
board state from memory.

## Operating Contract

The main agent owns the global objective and reasoning state. Subagents own
bounded execution nodes only. Do not let child-agent intermediate context become
the source of truth.

The division of labour is enforced by the host, not by convention:

- Subagents never receive the `longtask_*` board tools. They execute one bounded
  item and return a claim-evidence report; the main agent attaches and verifies
  it. Do not try to make a child update the board.
- Two state axes, two owners. `execution` (none | queued | running | reported |
  failed | timeout | cancelled) is the runtime's record of a dispatched run and
  is not yours to set. `resolution` (open | in_progress | resolved | cancelled)
  is your judgement, moved with `longtask_update_node`.
- **Attaching a report does not resolve the item.** `longtask_attach_report`
  records the child's report and marks the execution reported; the item stays
  open. A child's `status: success` is a claim, not a verdict.
- `resolved` means the result is back AND sufficiently checked. With
  `longtask.require_verification_before_unlock` enabled, an accepted verdict from
  `longtask_verify_node` is required first. Dependents unlock on `resolved`, so
  resolving on a missing or rejected verdict carries downstream work forward on
  an unverified claim.

For complex work:

1. Create or read a longtask board.
2. Represent the work as a DAG of small items with explicit dependencies.
3. Use `longtask_next` to select ready items in dependency order.
4. Delegate ready items with `delegate_task` — pass the report schema below as
   `output_schema` so the child returns structured JSON.
5. Require each subagent to return a claim-evidence report.
6. Attach reports to the board with `longtask_attach_report`.
7. Verify reports with `longtask_verify_node`.
8. Move the item to `resolved` with `longtask_update_node` once the verdict
   supports it.
9. Continue until every item is resolved or cancelled, or explicitly blocked on
   user input.

## Complexity Heuristic

Use the board when any of these are true:

- the task naturally has three or more steps;
- the task has dependencies between steps;
- the task needs separate research, implementation, and verification tracks;
- the task may run long enough for context compression;
- multiple subagents can safely work on independent nodes;
- the user asks for a durable plan, report, audit, migration, or investigation.

For simple one-shot tasks, answer or act directly.

## Node Shape

Each node should be self-contained:

- `node_id`: short stable id such as `N1`, `research_api`, or `verify_docs`.
- `goal`: concrete outcome the node must produce.
- `dependencies`: node ids that must be done first.

Good nodes have a clear output, bounded scope, and enough context for a
subagent to work without reading the parent conversation.

## Subagent Report Format

Ask subagents to finish with JSON-compatible content in this shape:

```json
{
  "status": "success|partial|failed|timeout",
  "claims": [
    {
      "claim": "What was found or changed.",
      "evidence": [
        {
          "kind": "file|command|url|observation",
          "ref": "Path, command, URL, or stable identifier.",
          "quote": "Short excerpt or result.",
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

Timeouts and failures still need a report. The report should say what was
attempted, what evidence was collected, and what remains uncertain.

The `status` field is the child's own label. It is recorded as advisory
(`report_status`) and never resolves the item — resolution is your explicit call
after verification.

## Main-Agent Discipline

- Keep the user's current objective and constraints in the board.
- Prefer board state over memory of the conversation when they conflict.
- Do not resolve an item without concrete evidence or an explicit user
  decision.
- Do not unlock downstream work until the upstream report has a verification
  result.
- When LLM verification is configured, treat its verdict as the final verifier
  verdict, but keep deterministic verifier details as supporting diagnostics.
- Do not delegate vague nodes; rewrite them first.
- Use the board to recover after compression or interruption.
- Keep parent-facing summaries compact; full evidence belongs in reports.
