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
├── topics.yaml             # interests, positive/negative terms, priority
├── research-profile.yaml   # long-term agenda, beliefs, questions, gaps
├── sources.yaml            # provider-backed Source Catalog
├── library.db              # items, evidence, runs, recommendations, feedback
├── scout/                  # staged Codex discovery results
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
4. Ranking combines topic relevance, source quality, novelty, open questions,
   and user feedback.
5. Reports and `/paper` render stored results; they do not fetch the web.
6. The low-frequency Scout asks Codex to discover terminology and sources not
   covered by the deterministic collectors, then stages validated suggestions
   for review.

Keyword lists are signals, not a complete allowlist. Provider search uses topic
names and `include` terms for recall, while ranking also uses open questions,
source evidence, profile agenda, and discovered term suggestions. Scout exists
specifically to reduce blind spots caused by changing terminology.

## Configuration

### Topics

Edit `<HERMES_HOME>/research-copilot/topics.yaml` to add or revise a topic:

```yaml
topics:
  - id: reliable-agents
    name: Reliable Agents
    status: active
    priority: 0.9
    description: Reliability of agents working on long-running tasks.
    include:
      - agent reliability
      - long-horizon agent
    exclude:
      - game agent
    open_questions:
      - How should recovery from trajectory errors be evaluated?
```

`status: dormant` keeps a topic without using it for collection. The CLI can
inspect and safely remove entries (with a timestamped backup):

```bash
hermes research topics list
hermes research topics remove <topic-id>
```

### Research profile

`research-profile.yaml` holds `long_term_agenda` entries with a core question,
current beliefs, open questions, and knowledge gaps. It grounds ranking and
Scout prompts; it is not rewritten automatically from fetched content.

```bash
hermes research profile show
hermes research profile remove-agenda <agenda-id>
```

### Sources

`sources.yaml` is a typed Source Catalog. Provider-specific validation lives in
`sources/providers/`; user-facing behavioral settings belong in YAML, while
credentials remain in the profile `.env`.

```bash
hermes research sources list
hermes research sources validate
```

Always validate after changing topics or sources.

## Commands and scheduled execution

Useful operator commands:

```bash
hermes research collect --dry-run
hermes research collect
hermes research recommend --dry-run
hermes research daily-report
hermes research health
hermes research doctor
hermes research scout --dry-run
```

Repository-owned cron entry points live in `research_copilot/scripts/` and run
as allowlisted modules, for example:

```text
module:research_copilot.scripts.library_collect
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
- `scout.py`: bounded Codex discovery with validated staged output.
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
