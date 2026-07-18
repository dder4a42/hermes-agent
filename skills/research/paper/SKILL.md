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
hermes research doctor
hermes research health
```

Collect:

```bash
hermes research collect --dry-run
hermes research collect --source <source-id> --dry-run
hermes research collect
```

Rank and recommend:

```bash
hermes research recommend --dry-run
hermes research recommend
```

Always dry-run source/config changes before a persisted collection. A dry-run
may perform network reads, but it must not change the Library, source runs or
recommendations.

## Collection architecture

The Source Catalog controls every source's provider, enabled state, tier,
topic scope, retry policy and budgets. The Source Runner applies global,
per-source and per-topic limits, then records each run. Providers only perform
network access and parsing; they never write state.

Bundled providers:

- RSS/Atom
- Semantic Scholar
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

## Recommendation procedure

1. Run `hermes research doctor`. Stop if the schema or required config is
   missing.
2. Run `hermes research health`. Mention degraded/stale/noisy sources when
   their state materially limits confidence.
3. Run `hermes research recommend --dry-run`.
4. If no item clears the configured threshold, respond with exactly
   `[SILENT]` for an automated delivery job.
5. For an eligible item, read `references/format.md` and the profile's
   `research-profile.yaml` when personalized context is needed.
6. Explain what the source claims separately from your own synthesis. Connect
   the signal to an exact profile open question or belief; never invent one.
7. Run `hermes research recommend` only when the recommendation should be
   persisted. The CLI writes the recommendation and Research Item state in one
   transaction.
8. Output only the user-facing brief for delivery jobs.

Do not calculate weighted scores manually. The deterministic ranker stores an
explainable breakdown for topic relevance/priority, open-question match,
source quality, independent-source confirmation, freshness, actionability and
source-saturation penalty.

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
- Do not equate recommended, saved and read states.

See `references/format.md` for the brief format and
`references/profile-aware-research-copilot.md` for the research-profile model.
