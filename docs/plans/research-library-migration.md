# Research Library migration and refactor plan

Status: accepted for implementation
Migration policy: intentionally incompatible
Code owner boundary: bundled code stays in the repository; profile state stays under `HERMES_HOME`

## Outcome

Research Copilot keeps its product name. Its storage and collection subsystem
becomes the Research Library. The refactor replaces the profile-copied fetcher
and append-only JSONL candidate pool with a repository-owned source pipeline and
a profile-scoped SQLite library.

The target flow is:

```text
Source Catalog -> Source Provider -> Source Runner -> Discovery
               -> identity/merge -> Research Item -> ranking/recommendation
```

## Scope decisions

- Do not preserve the legacy candidate JSONL schema, source registry schema,
  Python fetcher functions, behavioral environment variables, or cron jobs.
- Preserve topics, the research profile, recommendations and any user feedback.
- Archive all legacy data, but import only records referenced by recommendations
  or feedback. Do not import the noisy historical candidate backlog.
- Move directly to SQLite; do not add a temporary JSONL v2 or dual-write path.
- Keep arXiv disabled by default and require explicit source budgets to enable it.
- Do not add new upstream sources until existing providers are governed uniformly.
- Do not add a core model tool. Expose management through `hermes research` CLI
  commands and the bundled research skill.

## Ownership and filesystem boundary

Repository-owned code:

```text
research_copilot/sources/
research_copilot/library/
research_copilot/ranking/
research_copilot/health/
research_copilot/migration/
```

Profile-owned state:

```text
<HERMES_HOME>/research-copilot/library.db
<HERMES_HOME>/research-copilot/sources.yaml
<HERMES_HOME>/research-copilot/topics.yaml
<HERMES_HOME>/research-copilot/research-profile.yaml
<HERMES_HOME>/research-copilot/backups/
<HERMES_HOME>/research-copilot/exports/
```

No bundled Python business logic may be copied into the profile. If the current
cron implementation still requires a script, a generated shim may only import
and invoke the repository CLI. Bundled skills load from the repository; profile
skills are explicit user overrides and must be reported by `research doctor`.

## Target components

### Source layer

- `SourceDefinition`: provider, tier, topics, options, retry policy and budgets.
- `SourceCatalog`: strict YAML loading and validation.
- `SourceProvider`: network access and parsing only.
- `SourceRunner`: global/per-source budgets, retries, filtering, persistence and
  run accounting.
- `SourceRun`: fetched/new/merged/unchanged/filtered counts, timing and errors.

### Library layer

- `ResearchItem`: the stable, merged library entity.
- `Discovery`: evidence that one source found one item during one run.
- Stable internal item IDs; DOI/arXiv/S2/URL values remain external identifiers.
- Identity priority: DOI, arXiv without version, Semantic Scholar ID, normalized
  URL, then normalized title + first author + year.
- Duplicate discoveries upsert sources, topics, identifiers and better metadata.
  Fuzzy title matches produce audit findings and never silently merge.

### Database

SQLite tables:

```text
schema_metadata, sources, source_topics, source_runs,
research_items, item_identifiers, item_sources, item_topics,
recommendations, feedback_events
```

Enable foreign keys, WAL and a busy timeout. Recommendation creation plus item
status change, feedback plus status change, item merge and migration imports are
transactional. Timestamps are UTC ISO 8601 with an explicit offset.

### Ranking and health

Ranking stores an explainable breakdown for topic relevance/priority, open
question match, source quality, independent-source confirmation, freshness,
actionability and source saturation. Source tier comes from the catalog.

Health distinguishes `healthy`, `quiet`, `degraded`, `noisy`, `stale` and
`disabled`. It reports last success, success rate, rate limits, yield, merge
rate and recommendation conversion.

## CLI contract

```text
hermes research init
hermes research doctor
hermes research sources list|show|validate|test|enable|disable
hermes research collect [--source ID] [--dry-run]
hermes research library list|show|audit|archive
hermes research recommend [--dry-run]
hermes research health
hermes research migrate-to-library --dry-run|--apply
```

Cron invokes `hermes research collect`, `recommend` and `health`. Delivery stays
disabled during migration validation.

## Implementation batches

### Batch 0 - baseline

- Separate or preserve unrelated dirty-worktree changes.
- Record this plan and the current cron/profile inventory.
- Fix real-data health-report timestamp handling.
- Add reduced, anonymized legacy fixtures.

Exit: current tests pass and health reads the live profile without crashing.

### Batch 1 - Library foundation

- Add SQLite schema, connection lifecycle, repositories and transactions.
- Add identity normalization and deterministic merge rules.
- Test constraints, rollback, idempotency and multiple source/topic merges.

Exit: two discoveries of one paper produce one item with both evidence records.

### Batch 2 - Source Catalog and Runner

- Add strict source/topic configuration models and provider registry.
- Add global, per-source and per-topic budgets, retries and run accounting.
- Add dry-run with no state mutation.

Exit: invalid catalogs fail clearly, budgets are enforced, and one provider
failure does not corrupt or prevent accounting for the other providers.

### Batch 3 - Providers

Migrate in this order: RSS, Semantic Scholar, Hugging Face, Tavily, GitHub,
AlphaXiv, Newsletter, arXiv. Each provider gets fixture-based contract tests for
success, empty results, malformed responses, timeouts, 403 and 429 behavior.

Exit: providers do not write storage or mutate pipeline state directly.

### Batch 4 - ranking, recommendation and health

- Implement explainable scoring and source saturation control.
- Make recommendation/state writes transactional.
- Add source health and `research doctor` provenance output.

Exit: health works on real data and recommendations have reproducible breakdowns.

### Batch 5 - CLI, skill and cron

- Wire `hermes research` commands.
- Make the bundled research skill call the CLI.
- Replace copied profile scripts with command cron entries or minimal shims.

Exit: deleting legacy profile paper scripts does not break Research Copilot.

### Batch 6 - migration and cutover

- Create a timestamped backup with a SHA-256 manifest and cron inventory.
- Import topics/profile plus recommendation- or feedback-referenced items only.
- Produce a legacy-ID to ResearchItem-ID report.
- Validate, dry-run providers, collect, health and recommendation before enabling
  new cron jobs.

Exit: preserved records are connected, ordinary legacy noise is absent, and the
new cron jobs run without message delivery.

### Batch 7 - cleanup

- Remove the monolithic fetcher, legacy JSONL storage and behavioral env reads.
- Remove migration-only entry points after the observation window.
- Update the data contract, operations documentation and architecture guide.

Exit: no bundled Research Copilot business code lives below `HERMES_HOME`.

## Backup and rollback

Migration must stop unless backup and manifest verification succeed. Keep the
legacy directory and cron definitions through the observation window. Rollback
stops new cron jobs, preserves the new database for diagnosis, and restores the
legacy directory and cron inventory. New SQLite records are not reverse-written
to legacy JSONL.

## Completion criteria

- Every enabled source is catalog-managed and budgeted.
- One item supports multiple sources and topics without duplication.
- Recommendation and status data cannot diverge transactionally.
- `research health` and `research doctor` work against the live profile.
- Source runs explain requests, yield, merges, filtering and errors.
- arXiv is disabled by default and cannot run without bounded configuration.
- Cron does not depend on untracked profile business logic.
- Migration backup, manifest and selected-import report are reproducible.
- Tests cover real profile paths, provider contracts and database transactions.
