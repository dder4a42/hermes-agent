---
name: personal-english-learning
description: Use when a learner wants to assess, expand, or review English vocabulary with a private profile-local SQLite learning history. Prioritize high-frequency concrete word senses, Chinese-supported explanations, small daily loads, and deterministic review scheduling.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [education, english, vocabulary, spaced-repetition, sqlite]
    requires_toolsets: [terminal]
    category: productivity
---

# Personal English Learning

## Overview

This skill manages a private, vocabulary-first English learning loop. It is
designed for a learner who benefits from Chinese explanations and needs to
build high-frequency vocabulary before long academic reading or TOEFL tasks.

The SQLite database is the source of truth. Never invent review dates or edit
the database directly. Use the bundled CLI for every state change.

## When to Use

Use this skill when the user asks to:

- assess their current English vocabulary;
- learn high-frequency words or concrete word senses;
- review due English vocabulary;
- record whether a word is known, uncertain, or unknown;
- inspect vocabulary-learning progress;
- create a small daily English vocabulary plan.

Do not use it for general translation, complete essay generation, or an
official TOEFL score prediction.

## Runtime

The script is installed with the skill:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py <command>
```

It stores data under the active profile:

```text
$HERMES_HOME/personal-english-learning/learning.db
```

Commands return JSON. Treat a non-zero exit or `"ok": false` as a failure and
show the error to the user without claiming that learning state was saved.

## Workflow

### 1. Initialize

Before the first session, run:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py init
```

Completion criterion: the result contains `"ok": true` and a database path.

### 2. Import vocabulary

Import a prepared JSONL vocabulary source:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  import-jsonl /path/to/vocabulary.jsonl
```

Each line must identify a concrete sense, not only a spelling:

```json
{"lemma":"address","part_of_speech":"verb","definition_en":"to deal with a problem","definition_zh":"处理，应对","frequency_rank":812,"source":"my-list","source_sense_id":"address.v.deal"}
```

Report invalid-line errors. Do not silently treat a partially failed import as
fully successful.

### 3. Run the initial assessment

Fetch a reproducible sample across frequency bands:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  assessment-sample --per-band 10 --seed 0
```

Present one item at a time. Give the learner three choices:

```text
认识 / 不确定 / 不认识
```

Map them to `known`, `unsure`, and `unknown`. Record the response with the exact
`sense_id` and `frequency_band` returned by the sample:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  assessment-record \
  --sense-id SENSE_ID \
  --frequency-band 1-1000 \
  --response unsure \
  --event-id UNIQUE_EVENT_ID
```

Do not reveal the definition before the learner answers. After saving, show the
Chinese core meaning, short English definition, and one brief explanation.

### 4. Build the daily plan

Use small defaults for a basic learner:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  daily-plan --new-limit 8 --review-limit 30
```

Always handle `reviews` before `new_items`. If the effective new limit is lower
than requested, explain briefly that review backlog reduced today's new words.
Never override the returned limit by adding extra words conversationally.

### 5. Review one card at a time

For recognition cards, show the word and a short example or ask for the core
meaning. For recall cards, show the meaning and ask for the English expression.
Do not show the answer in the question.

After the learner answers, give concise feedback, then record exactly one
rating:

- `again`: wrong or no recall;
- `hard`: correct only with substantial hesitation or hints;
- `good`: correct without a material hint;
- `easy`: immediate and confident recall.

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  review \
  --card-id CARD_ID \
  --rating good \
  --idempotency-key UNIQUE_REVIEW_ID \
  --answer-text "learner answer" \
  --hint-count 0
```

Reuse the same idempotency key when retrying after an uncertain transport
result. Never generate a second key for the same learner answer.

### 6. Show progress

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py stats
```

Describe recognition and recall separately. Do not interpret the values as an
official vocabulary size, CEFR level, or TOEFL score.

## Teaching Style for a Basic Learner

- Keep the initial daily load at five to ten new senses.
- Explain the current core meaning before listing secondary meanings.
- Use Chinese support plus a short English definition.
- Prefer a common collocation and short example over a long etymology.
- Introduce production only after recognition is reasonably stable.
- Treat `不确定` as useful evidence, not as a wrong answer.
- Stop adding new items when the CLI reports a review backlog clamp.

## Safety and Data Rules

- Learning state is private and profile-local.
- Do not import scraped commercial dictionaries or commercial TOEFL questions.
- Preserve `source` and `source_sense_id` on every imported sense.
- Never run SQL from user-provided text.
- Never delete or reset the database without explicit user confirmation.
- Do not claim a review was recorded unless the CLI returned success.

## Common Pitfalls

1. Saving only a lemma. A learner can know one meaning and miss another; always
   save and review a concrete sense.
2. Showing too much information. Keep word families and etymology secondary for
   a basic learner.
3. Grading hesitation as fully wrong. Use `hard` when the answer is correct but
   effortful.
4. Creating duplicate review events after a timeout. Retry with the same
   idempotency key.
5. Adding conversational bonus words. The deterministic daily plan owns the
   workload limit.

## Verification Checklist

- [ ] Every state mutation used the CLI
- [ ] Every learned item includes a concrete sense and source
- [ ] Assessment answers were recorded before definitions were revealed
- [ ] Due reviews were presented before new items
- [ ] One learner answer produced exactly one idempotent review event
- [ ] Feedback did not claim an official vocabulary, CEFR, or TOEFL score
