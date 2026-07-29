# Personal English Learning MVP execution plan

Status: in progress

## Outcome

Build a local, vocabulary-first English learning loop for one Hermes profile:

```text
frequency-band assessment
  -> daily new word senses
  -> recognition and recall reviews
  -> graded short-text coverage and target confirmation
  -> immutable learning evidence
  -> deterministic scheduling
  -> weekly progress data
```

The MVP is an optional Hermes skill backed by SQLite. It does not add a core
model tool, mutate the system prompt during a conversation, require Redis, or
introduce a second agent runtime.

## Product boundary

The first release targets a learner who needs to expand high-frequency
vocabulary before long-form academic reading or TOEFL simulation.

Included:

- one profile-local SQLite database;
- word senses as the learning unit;
- JSONL vocabulary import with frequency ranks and source attribution;
- frequency-band assessment using `known`, `unsure`, and `unknown` answers;
- recognition and recall cards;
- daily review and new-item capacity limits;
- immutable review and assessment events;
- deterministic short-text coverage and target-sense confirmation;
- JSON CLI responses suitable for Hermes skills and cron jobs.

Deferred:

- bundled dictionary dumps;
- LLM-assisted word-sense disambiguation;
- writing correction;
- pronunciation audio;
- FSRS parameter fitting;
- TOEFL task simulation;
- web/mobile UI and multi-device synchronization.

## Architecture

```text
Hermes skill / cron
       |
       v
english_learning.py CLI
       |
       +-- learning service
       +-- deterministic scheduler
       +-- SQLite repository (WAL + foreign keys)
```

Runtime data lives under the active profile:

```text
<HERMES_HOME>/personal-english-learning/learning.db
```

Tests must redirect this path to a temporary directory. No test may read or
write a real user profile.

## Data contracts

### Word sense import

One JSON object per line:

```json
{
  "lemma": "address",
  "part_of_speech": "verb",
  "definition_en": "to deal with a problem",
  "definition_zh": "处理，应对",
  "frequency_rank": 812,
  "source": "example",
  "source_sense_id": "address.v.02"
}
```

The stable local identity is the tuple `(source, source_sense_id)`. When a
source has no stable sense identifier, the importer derives a deterministic
identifier from normalized lexical fields and reports that it did so.

### Evidence and projections

Assessment and review rows are append-only facts. `user_knowledge_states` is a
rebuildable projection containing recognition and recall estimates plus
evidence counts. The MVP does not infer production or transfer scores without
production evidence.

### Scheduling

The initial scheduler is deterministic and deliberately small. It is not
labelled FSRS. It records enough event data to migrate to FSRS later without
discarding history.

Ratings are `again`, `hard`, `good`, and `easy`. Every review command accepts an
idempotency key. Replaying the same event must return the original result and
must not advance the card twice.

## Delivery phases

### Phase 1: local learning core

Deliver:

- schema creation and migration metadata;
- vocabulary import and deduplication;
- assessment sampling by frequency band;
- assessment evidence recording;
- daily due/new plan;
- review recording and rescheduling;
- progress statistics.

Exit criteria:

- a real temporary SQLite database completes import -> assessment -> daily
  plan -> review in an end-to-end test;
- duplicate imports and duplicate review events are idempotent;
- review backlog deterministically reduces the new-word allowance;
- every returned learning item identifies its source and concrete sense.

### Phase 2: graded reading

Deliver short-text ingestion, lexical coverage calculation, contextual sense
selection, and three-to-five target senses per text. This phase starts only
after Phase 1 evidence and scheduling contracts are stable.

Status: deterministic foundation complete. Unique lexical matches are ranked
using assessment gaps, frequency and repetition. Ambiguous matches are returned
with their candidate senses and require an explicit decision; the MVP does not
pretend that spelling alone is contextual disambiguation.

### Phase 3: lightweight production

Add sentence completion, short sentence production, error evidence, and
production projections. Do not start with long summaries.

### Phase 4: automation

Add daily and weekly cron blueprints after the CLI is stable. Cron prompts call
the skill; they never edit SQLite directly.

### Phase 5: TOEFL 2026

Add versioned exam profiles and independently authored practice tasks. Reading
and writing come before listening and speaking.

## Verification

- Unit tests cover schema constraints, import identity, assessment bands,
  backlog limits, scheduling transitions, and idempotency.
- End-to-end tests use the public CLI/service boundary with a temporary
  `HERMES_HOME` and real SQLite I/O.
- Tests assert behavioral relations rather than exact database row counts or
  schema-version literals unless the literal is itself the contract.
- Skill frontmatter and script paths pass the repository skill validators.
- No core tool schema, global dependency, or user-facing environment variable
  is added.

## Next decision gates

Before rolling Phase 2 out for daily use, choose the first vocabulary source and
validate its import quality on at least 500 common word senses; fixture-backed
development of the deterministic reading path does not waive that data-quality
gate. Before FSRS adoption, collect enough real review history to compare the
new scheduler against the deterministic MVP baseline.
