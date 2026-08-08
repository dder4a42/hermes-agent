---
name: paper
description: "Personal Research Copilot backed by the profile-scoped Research Library."
version: 2.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    category: research
    tags: [paper, research, recommendation, copilot, library]
---

# Research Copilot — Paper Skill

Use this skill to collect, rank, recommend and discuss research signals. A
signal may be a paper, lab post, technical report, repository, benchmark,
newsletter item or community discussion.

## Operating contract

Research Copilot uses repository-owned Python code and profile-owned state:

```text
<HERMES_HOME>/research-copilot/library.db
<HERMES_HOME>/research-copilot/sources.yaml
<HERMES_HOME>/research-copilot/topics.yaml
<HERMES_HOME>/research-copilot/research-profile.yaml
```

Never read or edit legacy `candidates.jsonl`, `recommendations.jsonl`,
`source_registry.json` or copied `paper-fetch.py` scripts. Do not manipulate
SQLite directly. Use the CLI, which enforces identity merging, transactions,
source budgets and profile isolation.

`HERMES_HOME` is authoritative. Never hardcode `~/.hermes`, because named
profiles must remain isolated.

## CLI

Inspect and validate:

```bash
hermes research sources list
hermes research sources validate
hermes research config validate
hermes research doctor
hermes research health
hermes research evidence list --status pending
hermes research profile-proposal --dry-run
hermes research wiki export
hermes research wiki lint
```

Collect:

```bash
hermes research collect --dry-run
hermes research collect --source <source-id> --dry-run
hermes research collect
```

Run low-frequency discovery:

```bash
hermes research scout --dry-run
hermes research scout
```

Scout is one isolated `research-copilot` profile one-shot with only the `web`
toolset. It must research sequentially and return schema-validated JSON to
staging. Never replace it with an ungrounded direct chat-completions call, and
never give the Scout terminal, file, or delegation tools. Create the worker
profile with `hermes profile create research-copilot --clone --no-alias` so it
inherits the configured search credential without sharing session history.

For a selected item's deep research, read
`references/deep-research-artifact.md`. Produce the schema-v1 YAML/JSON
artifact, validate it, then import it with `--apply`. Never promote raw CLI
stdout or a transcript into the Wiki. Structured artifact import is the only
automatic path to `deep_researched`; it remains independent of user learning.

Rank and recommend:

```bash
hermes research triage
hermes research recommend --dry-run
hermes research recommend
```

Always dry-run source/config changes before a persisted collection. A dry-run
may perform network reads, but it must not change the Library, source runs or
recommendations.

Record profile-linked evidence only after reading the Research Item. Use
`/paper start <id>` when beginning, `/paper read <id>` only after finishing,
and `/paper synthesize <id>` after producing a structured synthesis. Use
`/paper study [n]` to reopen recent unread learning packages. Then use
`hermes research evidence add` with an exact
belief/question id and one of these claim types:

- `source_claim`: what the cited source actually states.
- `agent_inference`: a bounded inference from the cited material.
- `personal_take`: a profile-specific interpretation or research judgment.

Every record begins as `pending`. Review the underlying source before running
`hermes research evidence review <id> --accept` or `--reject`. A profile
proposal may summarize accepted evidence and suggest confidence changes, but
it must never edit `research-profile.yaml`; belief updates require explicit
user confirmation.

## Collection architecture

The Source Catalog controls every source's provider, enabled state, tier,
topic scope, retry policy and budgets. The Source Runner applies global,
per-source and per-topic limits, then records each run. Providers only perform
network access and parsing; they never write state.

Bundled providers:

- RSS/Atom
- Semantic Scholar (provider retained, Source Catalog entry disabled)
- Hugging Face Daily Papers
- Tavily over catalog-constrained domains
- GitHub Trending
- AlphaXiv
- Gmail newsletters under the configured label
- arXiv, disabled by default

The Library merges a signal discovered from multiple providers into one
Research Item while preserving independent source evidence and topic matches.
Do not treat repeated discoveries as duplicate noise: independent confirmation
is a ranking signal.

Query-backed providers use the shared fair planner; do not manually reorder
topics to influence request budgets. Identity merging prefers DOI/arXiv and may
use exact-title reconciliation only when author/year evidence agrees and strong
identifiers do not conflict. Treat `item_merge_evidence` as the audit record;
never merge records by fuzzy title alone.

Every fetched result, including a Semantic Scholar/arXiv/Tavily query result,
must pass the shared content-level topic gate before persistence. Query
provenance is not itself proof of relevance. Matching normalizes common plural,
hyphen, and word-order variations, applies `exclude_terms`, and stores the
content terms that justified the label. Diagnose high filtering by reviewing
topic match/exclude terms; do not bypass the gate or automatically broaden all
queries.

## LLM Wiki workflow

The profile's SQLite Library is the evidence source of truth. The configured
Obsidian Research Library is a compiled and curated view, not a second database.
After collection or substantial manual research, run `hermes research wiki
export`, then `hermes research wiki lint`. The exporter may refresh generated
frontmatter, summaries, navigation, and source metadata; it must preserve the
Agent-owned `## 解读` and `## 相关笔记` sections. Fix lint errors before relying
on the index for synthesis.

Configure the destination with `hermes research wiki configure --vault <path>`.
This writes `research_copilot.wiki` to the active profile's `config.yaml`;
never store the vault path in `.env`.

## Recommendation procedure

1. Run `hermes research doctor`. Stop if the schema or required config is
   missing.
2. Run `hermes research health`. Mention degraded/stale/noisy sources when
   their state materially limits confidence.
3. Run `hermes research triage` to inspect the bounded candidate queue. Do not
   deep-read every collected item.
4. Run `hermes research recommend --dry-run`.
5. If no item clears the configured threshold or the recommendation budget is
   exhausted, respond with exactly
   `[SILENT]` for an automated delivery job.
6. For an eligible item, read `references/format.md` and the profile's
   `research-profile.yaml` when personalized context is needed.
7. Explain what the source claims separately from your own synthesis. Connect
   the signal to an exact profile open question or belief; never invent one.
8. Run `hermes research recommend` only when the recommendation should be
   persisted. The CLI records a recommendation event without changing the
   Research Item's workflow state.
9. Output only the user-facing brief for delivery jobs.

Do not calculate weighted scores manually. The deterministic ranker stores an
explainable breakdown for primary/secondary topic relevance, profile agenda,
open-question and knowledge-gap matches, source quality, independent-source
confirmation, freshness, actionability and source-saturation penalty. Use the
stable ids emitted by the ranker when explaining why an item was selected.

## Source policy

- Source tier is configured in `sources.yaml`; it is not hardcoded in the
  skill or stamped permanently into an item.
- Company and researcher posts are useful frontier signals but are not
  peer-reviewed evidence. Preserve their bias/context notes in analysis.
- Gmail credentials remain in `<HERMES_HOME>/.env`; never place credentials in
  `sources.yaml` or a prompt.
- arXiv is broad and noisy. Keep it disabled unless the user explicitly enables
  it with bounded request, item and per-topic budgets.
- GitHub repositories are projects, not papers.

## Health semantics

- `healthy`: recent successful runs with usable output.
- `quiet`: successful, but no recent matching items.
- `degraded`: failures dominate the observation window.
- `noisy`: high new-item volume with negligible recommendation conversion.
- `stale`: no recent completed run.
- `disabled`: intentionally not scheduled.

`fetched` means upstream records parsed; `filtered` means rejected by topic or
budget; `new`, `merged` and `unchanged` describe Library effects.

## Output style

- Default to concise Chinese narrative while retaining standard English
  technical terms.
- Distinguish “authors/source claim” from “analysis”.
- Prefer one strong signal over a link digest.
- Include actionable implications for the user's research agenda.
- Use conservative Markdown-lite for mobile messaging clients.
- Recommendation is an event, not a workflow state. Human learning is tracked
  independently as `unseen/saved/reading/read/skipped`; Agent analysis is
  tracked as `none/triaged/deep_researched/synthesized`. Never infer that the
  user read an item because the Agent analyzed it, or vice versa.

See `references/format.md` for the brief format and
`references/profile-aware-research-copilot.md` for the research-profile model.
