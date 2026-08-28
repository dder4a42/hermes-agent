# Research Copilot

Research Copilot is Hermes' profile-scoped research discovery pipeline. It
collects candidate material from configured providers, stores normalized items
in a SQLite Research Library, ranks them against the user's topics and research
profile, and exposes the result through cron jobs, the CLI, and `/paper` in
messaging gateways.

This directory is the canonical implementation. Files under
`docs/research-copilot/` that describe `candidates.jsonl`,
`recommendations.jsonl`, or profile-local Python scripts document the legacy
pipeline and are not the runtime contract for the Library implementation.

## Runtime model

Each Hermes profile owns an independent Research Copilot state directory:

```text
<HERMES_HOME>/research-copilot/
├── topics.yaml             # discovery queries and classification terms
├── research-config.yaml    # long-term agenda, beliefs, questions, gaps
├── sources.yaml            # provider-backed Source Catalog
├── library.db              # items, reviewable evidence, runs, recommendations, feedback
├── reports/profile-proposals/ # non-mutating belief/question update proposals
├── scout/                  # staged Hermes deep-research results
└── backups/                # migration and preference-edit backups
```

`HERMES_HOME` is `~/.hermes` for the default profile and
`~/.hermes/profiles/<name>` for a named profile. Runtime code resolves it via
`hermes_constants.get_hermes_home()`; it must never read another profile's
tree.

The pipeline is deliberately split into stages:

1. Providers in `sources/providers/` fetch source-specific records.
2. `SourceRunner` normalizes and merges evidence into `library.db`.
3. Newsletter enrichment resolves tracked URLs before promotion.
4. Ranking combines topic relevance and source evidence with agenda priority
   and open questions from the research profile.
5. Reports and `/paper` render stored results; they do not fetch the web.
6. The low-frequency Scout asks Codex to discover terminology and sources not
   covered by the deterministic collectors, then stages validated suggestions
   for review.

Keyword lists are signals, not a complete allowlist. Provider search uses
`search_queries`, while deterministic classification uses `match_terms` and
`exclude_terms`. Ranking gets priorities and open questions from the profile,
not from topics. Scout exists
specifically to reduce blind spots caused by changing terminology.

Search-backed providers share a deterministic query planner. It round-robins
topics before issuing a second query for any topic, rotates the starting topic
by source and day, and merges duplicate queries while preserving all topic
attribution. Request budget order therefore does not permanently favor the
first topic in YAML.

## Configuration

### Topics

Edit `<HERMES_HOME>/research-copilot/topics.yaml` to add or revise a topic:

```yaml
schema_version: 2
topics:
  - id: reliable-agents
    name: Reliable Agents
    status: active
    description: Reliability of agents working on long-running tasks.
    search_queries:
      - reliable research agents
    match_terms:
      - agent reliability
      - long-horizon agent
    exclude_terms:
      - game agent
```

`status: inactive` keeps a topic without using it for deterministic collection. The CLI can
inspect and safely remove entries (with a timestamped backup):

```bash
hermes research topics list
hermes research topics remove <topic-id>
```

### Research config

`research-config.yaml` holds `long_term_agenda` entries linked through
`topic_ids`, with a priority, core question, current beliefs, open questions,
and knowledge gaps. It grounds ranking and
Scout prompts; it is not rewritten automatically from fetched content.

```yaml
schema_version: 2
long_term_agenda:
  - id: reliable-research-agents
    topic_ids: [reliable-agents]
    priority: 0.9
    core_question: How should long-running agents recover from errors?
    open_questions:
      - How should recovery from trajectory errors be evaluated?
    knowledge_gaps: []
```

Research Items keep three orthogonal signals. `workflow_state` remains a
compatibility/archival lifecycle; machine progress is
`none -> triaged -> deep_researched -> synthesized`; reader progress is
`unseen -> saved/reading -> read` with `skipped` as a separate outcome.
Recommendation records are independent events. Agent analysis never implies
that the user read an item, and reader feedback never fabricates Agent work.
Opening an older Library creates a consistent
`backups/library-schema-v<version>-*.db` snapshot before migration.

```bash
hermes research profile show
hermes research profile remove-agenda <agenda-id>
hermes research config validate
hermes research config migrate        # preview only
hermes research config migrate --apply
```

### Evidence and profile proposals

Only a user-read item or an Agent `deep_researched`/`synthesized` item may
become profile-linked evidence. Record the source's claim separately from agent inference and
personal interpretation, then explicitly accept or reject it:

```bash
hermes research evidence add <item-id> \
  --belief <belief-id> --relation challenges \
  --claim-type source_claim --strength 0.8 \
  --source-quality primary --claim "..." --rationale "..."
hermes research evidence list --status pending
hermes research evidence review <evidence-id> --accept --note "checked in paper"
hermes research profile-proposal --dry-run
hermes research profile-proposal
```

The generated proposal is a review artifact, not a write operation against
`research-config.yaml`. It records the current research-config hash, flags evidence
captured against an older revision, and keeps confidence suggestions visibly
heuristic. The user must decide whether and how to edit the profile.
Accepted evidence whose target was later removed is retained under
`orphaned_evidence` so profile drift cannot silently discard it.

For daily learning, `/paper study [n]` renders recent unread recommendations
as a three-minute summary, an explicit abstract-only evidence boundary, recall
questions and next actions. Use `/paper start <id>` when beginning and
`/paper read <id>` only after finishing.

Existing substantial Wiki analyses can be reconciled into machine state
without touching reader state:

```bash
hermes research wiki reconcile          # preview
hermes research wiki reconcile --apply
```

New deep-research runs should emit the schema-v1 artifact documented in the
paper skill, then use the explicit import boundary:

```bash
hermes research deep-research import artifact.yaml          # validate only
hermes research deep-research import artifact.yaml --apply  # idempotent import
hermes research wiki export
hermes research wiki lint
```

Artifacts are keyed by a content hash and compiled by exact Library item id;
terminal transcripts are never treated as knowledge artifacts.

### Sources

`sources.yaml` is a typed Source Catalog. Provider-specific validation lives in
`sources/providers/`; user-facing behavioral settings belong in YAML, while
credentials remain in the profile `.env`.

```bash
hermes research sources list
hermes research sources validate
```

Always validate after changing topics or sources.

Query-backed providers use a fair daily-rotated, round-robin query plan, but a
query result does not receive a topic merely because the upstream API returned
it. Every provider passes through the same title/summary content gate using
`match_terms` and `exclude_terms`. Matching normalizes hyphens, word order, and
common English plurals while still requiring every significant word in a
configured phrase. Persisted `matched_terms` therefore retain content evidence
in addition to query provenance.

Shared HTTP fetching retries transient network failures and truncated responses
such as `IncompleteRead`. Failed attempts still count against provider/global
request budgets.

## Identity and reconciliation

Canonical identity prefers DOI, then arXiv, Semantic Scholar, normalized URL,
and finally normalized title/author/year. Enriched project pages contribute DOI
and arXiv identifiers from citation metadata, JSON-LD, canonical links, and
paper links. A stronger identifier may promote an item's canonical key without
changing its stable Library item id.

When two records have no shared identifier, automatic reconciliation requires
an exact normalized title plus overlapping authors, or a compatible year with
at least one strong identifier. Conflicting DOI/arXiv/Semantic Scholar ids and
non-academic item-type pairs are never merged. Every guarded reconciliation is
stored in `item_merge_evidence` for audit.

## Commands and scheduled execution

Useful operator commands:

```bash
hermes research collect --dry-run
hermes research collect --source tavily --dry-run --show-items --max-requests 1 --max-new-items 5
hermes research collect
hermes research triage
hermes research recommend --dry-run
hermes research daily-report
hermes research health
hermes research doctor
hermes research evidence list --status pending
hermes research profile-proposal --dry-run
hermes research scout --dry-run
hermes research deep-research run --dry-run
hermes research deep-research run
```

Scout behavior belongs in `config.yaml`; credentials remain in `.env`:

```yaml
research_copilot:
  collection:
    failure_cooldown:
      threshold: 3
      base_minutes: 60
      max_hours: 24
  ranking:
    daily_triage_limit: 10
    daily_recommendation_limit: 1
    weekly_recommendation_limit: 3
    triage_card_max_chars: 200
    secondary_topic_bonus_cap: 0.10
  scout:
    profile: research-copilot
    timeout_seconds: 2700
  deep_research:
    profile: research-copilot
    timeout_seconds: 2700
```

Collection is incremental. RSS/Atom validators (`ETag` and `Last-Modified`)
are stored in the profile-scoped Library and reused for conditional requests;
HTTP 304 is a successful empty run. A source that fails repeatedly enters an
exponential cooldown after `threshold` consecutive failures. Successful and
partial runs clear the failure counter. `hermes research collect --dry-run`
bypasses cooldown and stored validators for diagnosis and never changes the
database. Cooldown state is visible in `health` and `doctor`.

Gmail newsletters use the selected mailbox's `UIDVALIDITY` plus `last_uid`.
When UIDVALIDITY is unchanged, only newer UIDs are searched and fetched. A
mailbox reset automatically falls back to the configured lookback window; a
failed UID is not crossed, so it is retried on the next run. Newsletter issues
are staged atomically and retain `(mailbox, UIDVALIDITY, UID)` provenance.
`health` reports only a compact cursor summary, while SourceRun metrics record
incremental mode and UID counts without storing message content.

Health distinguishes transport success from research usefulness. A non-staging
source that repeatedly fetches volume but filters at least 95% with no new or
merged items is `overfiltered`, not `healthy`; this points to topic terms,
source scope, or parser quality rather than network availability.

RSS parsing is namespace-agnostic across RSS and Atom variants. It prefers
canonical/original links, consumes `content:encoded`, Dublin Core creator/date,
Atom author/category fields, RFC 822 dates, GUID permalinks, and relative URLs.
SourceRun metrics separate entries discovered, items emitted, missing title or
link, duplicate links, parser-empty runs, topic rejection, and explicit
exclusion. `collect --dry-run --show-items` prints accepted and filtered samples
with rejection reasons, so parser repair and topic-term tuning remain separate.

Semantic Scholar remains a supported parser/identity source for historical
records, but it is disabled in migrated Source Catalogs. Enable it explicitly
in `sources.yaml` only if its API is intentionally restored.

Create the isolated Scout worker once, cloning the configured Tavily secret
and model/provider settings from the active profile:

```bash
hermes profile create research-copilot --clone --no-alias
```

Scout starts exactly one non-interactive child process equivalent to `hermes
-p research-copilot -z <prompt> -t web`. The explicit `web` toolset permits
only web search and extraction: no terminal, file mutation, delegation, or
subagents. The task searches and verifies sources sequentially, then its final
JSON is schema- and topic-validated before staging. A missing/broken worker
profile fails explicitly; Scout never falls back to the caller's profile.

Deep research uses the same isolated worker boundary but is not another
discovery pass. It selects only an already-recommended item whose agent
analysis is still `none` or `triaged`, researches that exact item, validates
the schema-v1 artifact, imports it idempotently, and compiles the matching Wiki
page by stable item id. It never changes `user_learning_status`. The standard
profile cron runs this once each Sunday and delivers the rendered study note.
Cron delivery is acknowledged through `deep_research_delivery_outbox`: an
unconfirmed or rate-limited learning card is replayed byte-for-byte on the next
run, without consuming another candidate or invoking the model. Only a
successful scheduler delivery receipt permits the next deep-research task.
A separate daily `research-deep-delivery-retry` job is silent when the outbox
is empty and otherwise retries only the pending card; it never invokes Hermes
research or selects another item.

`triage` is read-only and renders a bounded queue of inexpensive summary
cards. Ranking uses the primary topic plus a capped secondary-topic bonus;
profile agenda, open-question, and knowledge-gap ids are retained in the
explanation. `recommend` persists at most the configured daily and weekly
number of recommendation events. These settings are behavioral configuration,
not credentials, so they must not be placed in `.env`.

Repository-owned cron entry points live in `research_copilot/scripts/` and run
as allowlisted modules, for example:

```text
module:research_copilot.scripts.library_collect
module:research_copilot.scripts.library_deep_research
module:research_copilot.scripts.library_recommend
module:research_copilot.scripts.library_health
module:research_copilot.scripts.library_scout
```

Profiles do not copy these Python files. Editable installs and normal package
installs therefore execute the same version as the Hermes checkout/package,
avoiding profile/repository synchronization drift.

Messaging users can use `/paper topics`, `/paper history`, `/paper save`,
`/paper skip`, `/paper feedback`, `/paper health`, and `/paper now`.

## Package map

- `library/`: schema, identity, transactional repository, feedback records.
- `sources/`: catalog, provider registry, collectors, newsletter parsing.
- `enrichment/`: tracked-URL resolution and page metadata.
- `ranking/`: scoring policy and recommendation service.
- `reporting/`: user-facing reports from persisted Library state.
- `health/`: source-run health and installation/config provenance.
- `migration/`: auditable migration from the legacy JSONL pipeline.
- `scout.py`: bounded, linear Hermes deep research with validated staged output.
- `bootstrap.py`: profile initialization and cron installation.
- `commands.py`: `/paper` behavior shared by messaging surfaces.

## Development and verification

Run the focused suite:

```bash
.venv/bin/python -m pytest tests/research_copilot \
  tests/hermes_cli/test_research_copilot_cmd.py \
  tests/gateway/test_paper_slash_commands.py -q
```

Changes to paths, migration, source resolution, cron modules, or feedback must
exercise real imports and a temporary `HERMES_HOME`; do not rely only on mocked
unit paths. Preserve profile isolation and keep repository modules out of the
core model-tool schema.
