---
title: "Personal English Learning"
sidebar_label: "Personal English Learning"
description: "Use when a learner wants to assess, expand, or review English vocabulary with a private profile-local SQLite learning history"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Personal English Learning

Use when a learner wants to assess, expand, or review English vocabulary with a private profile-local SQLite learning history. Prioritize high-frequency concrete word senses, Chinese-supported explanations, small daily loads, and deterministic review scheduling.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/productivity/personal-english-learning` |
| Path | `optional-skills/productivity/personal-english-learning` |
| Version | `0.1.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `education`, `english`, `vocabulary`, `spaced-repetition`, `sqlite` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

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
- analyze a short English text for lexical coverage and learning targets;
- record whether a word is known, uncertain, or unknown;
- inspect vocabulary-learning progress;
- create a small daily English vocabulary plan.
- practice target words through contextual gaps and short original sentences.
- inspect the versioned 2026 TOEFL profile, calculate practice bands, or plan
  a small Reading/Writing practice block.

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

### 7. Analyze a short reading

Analyze pasted text or a UTF-8 text file:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  reading-analyze \
  --file /path/to/short-reading.txt \
  --title "Agent systems" \
  --source "user_text" \
  --target-limit 5
```

Interpret the result conservatively:

- `catalog_coverage` is the share of tokens matched by the imported vocabulary;
- `known_coverage` counts only unambiguous matches with recognition at or above
  the learning threshold;
- `targets` contains high-value unambiguous senses, ordered by assessed gaps,
  repetition and frequency;
- `ambiguous_matches` requires a learner or reliable contextual analysis to
  choose a sense.

Never present low catalog coverage as poor learner ability: it may only mean the
local vocabulary source is incomplete. Never automatically accept a sense from
`ambiguous_matches` based on spelling alone.

After showing three to five candidates, ask the learner which ones to save.
Record all decisions in one command:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  reading-confirm \
  --document-id DOCUMENT_ID \
  --decision SENSE_ID_1=accepted \
  --decision SENSE_ID_2=rejected
```

An accepted target creates source-linked encounters and, when needed, one
recognition card. Repeating the same confirmation is safe and does not duplicate
encounters or cards.

### 8. Practice lightweight production

Generate exercises only after the learner has accepted reading targets:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  production-plan --document-id DOCUMENT_ID --limit 6
```

The plan produces:

- `cloze`: an original reading sentence with the target surface replaced by
  `____`;
- `sentence`: a request for one short original sentence using the concrete
  target meaning.

Present one exercise at a time. Do not show `expected_answer` before a cloze
answer. For cloze exercises, omit `--outcome`; the CLI performs an exact,
case-insensitive check:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  production-submit \
  --exercise-id EXERCISE_ID \
  --answer-text "agent" \
  --idempotency-key UNIQUE_ATTEMPT_ID
```

For a short sentence, classify the answer using exactly one outcome:

- `correct`: target sense and sentence grammar are both acceptable;
- `partial`: target sense is understandable but grammar, collocation, or form
  needs a material correction;
- `incorrect`: wrong sense, unusable construction, or no meaningful attempt.

Give one concise, actionable feedback point and record it:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  production-submit \
  --exercise-id EXERCISE_ID \
  --answer-text "The agent work automatically." \
  --outcome partial \
  --feedback "Use third-person singular: works." \
  --idempotency-key UNIQUE_ATTEMPT_ID
```

For `partial` or `incorrect`, ask the learner to revise before showing a model
answer. Record the revision as a new immutable attempt linked to the first:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  production-submit \
  --exercise-id EXERCISE_ID \
  --answer-text "The agent works automatically." \
  --outcome correct \
  --revision-of-attempt-id ORIGINAL_ATTEMPT_ID \
  --idempotency-key UNIQUE_REVISION_ID
```

Never overwrite the original answer. A failed production attempt may create a
recall card, but it does not directly rewrite an existing review schedule.

### 9. Run daily and weekly automation

This skill declares a daily 08:00 automation blueprint. Installing the skill
only creates a Hermes suggestion; it never schedules a job without user
acceptance. Review and accept it through `/suggestions`, or create an equivalent
profile-local cron job explicitly.

The daily run must:

1. Call `daily-plan --new-limit 8 --review-limit 30` exactly once.
2. Present returned reviews before new items.
3. Respect `effective_new_limit`; never compensate with extra conversational
   vocabulary.
4. On Monday in the configured profile timezone, also call:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  weekly-report --days 7
```

The weekly report is an evidence summary, not a grading model. It contains
assessment responses, review ratings, reading encounters, production outcomes,
revision counts, cards created, current mastery projections, and current review
backlog. It does not infer completion rate because the MVP does not persist a
daily-plan assignment ledger.

Automation rules:

- Use only the active profile's `HERMES_HOME`; never pass another profile's
  database path.
- Never run SQL directly from a cron prompt.
- Retry a state-changing command only with its original idempotency key.
- `weekly-report` is read-only with respect to learning events and may be
  safely retried.
- If the database or CLI fails, deliver one concise error with the failing
  command and stop; do not fabricate a plan or report.
- Return `[SILENT]` only when there are no due reviews, no new items, and no
  Monday report to deliver.

### 10. Use the TOEFL 2026 practice profile

Inspect the bundled, versioned profile before discussing the current exam:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  toefl-profile
```

The profile records its effective date and ETS sources. Reading and Listening
are marked as two-stage adaptive; Writing and Speaking are linear. The MVP only
plans independently authored Reading and Writing practice. It does not simulate
adaptive routing, ship an ETS question bank, or implement Listening/Speaking
practice yet.

When four valid section bands are already available, calculate the overall
practice band deterministically:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  toefl-score \
  --reading 4.0 --listening 3.5 --speaking 3.5 --writing 4.0
```

Section inputs must use the 1.0–6.0 scale in 0.5 increments. Present
`legacy_comparable_total_range` only as a comparison range, never as an exact
0–120 conversion. Always preserve the returned disclaimer: this calculation is
personal learning feedback, not an official ETS score prediction.

Build a bounded practice block with the current Reading/Writing task labels:

```bash
python3 ~/.hermes/skills/productivity/personal-english-learning/scripts/english_learning.py \
  toefl-practice-plan --minutes 30 --section reading --section writing
```

Follow the returned order and minute budget. Generate original prompts suitable
for a basic learner; never reproduce or imply access to official test items.
`adaptive_simulation: false` is a deliberate boundary, not a missing score.

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
6. Treating a spelling match as word-sense disambiguation. Require an explicit
   choice whenever the analysis returns multiple senses.
7. Replacing the learner's answer with a correction. Save the original attempt,
   provide one focused hint, and link the learner's revision.
8. Letting the model invent a numeric production score. Submit only the fixed
   outcome enum; the backend owns its deterministic evidence value.
9. Scheduling on skill installation. A blueprint is a suggestion; require user
   acceptance before Hermes creates the cron job.
10. Computing weekly progress from chat memory. Always call the read-only report
    command so the summary reflects SQLite evidence.
11. Treating a practice band or legacy comparison range as an official score.
    Preserve the disclaimer and describe it as learning feedback.

## Verification Checklist

- [ ] Every state mutation used the CLI
- [ ] Every learned item includes a concrete sense and source
- [ ] Assessment answers were recorded before definitions were revealed
- [ ] Due reviews were presented before new items
- [ ] One learner answer produced exactly one idempotent review event
- [ ] Feedback did not claim an official vocabulary, CEFR, or TOEFL score
- [ ] Ambiguous reading matches were confirmed before entering the learning set
- [ ] Open production answers used a fixed outcome and retained the original text
- [ ] Revisions linked to the original attempt instead of replacing it
- [ ] Automation used the active profile and respected the returned plan limits
- [ ] Weekly summaries came from `weekly-report`, not conversational memory
- [ ] TOEFL guidance used the bundled profile version and preserved its disclaimer
