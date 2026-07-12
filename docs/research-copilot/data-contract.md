# Research Copilot data contract

Research Copilot state lives entirely under `<HERMES_HOME>/research-copilot/`.
Every profile has its own tree — Research Copilot never reads or writes another
profile's data. `HERMES_HOME` defaults to `~/.hermes` for the primary profile
and `~/.hermes/profiles/<name>` for named profiles; use
`research_copilot.storage.get_hermes_home()` to resolve it, never hardcode.

Files:

| Path | Kind | Purpose |
|---|---|---|
| `config.json` | JSON | Pipeline config, scoring weights, thresholds, policy flags |
| `topics.json` | JSON | Active research topics and their include/exclude/open-question tags |
| `research_profile.json` | JSON | The user's stance, long-term agenda, current beliefs |
| `source_registry.json` | JSON | Curated feeds/domains and their tier weights |
| `candidates.jsonl` | JSONL | Every item the fetcher has ingested. Grows monotonically |
| `recommendations.jsonl` | JSONL | The subset that was recommended to the user, one line per push |
| `interactions.jsonl` | JSONL | User feedback: save / skip / read / feedback / topic-adjustment |
| `state.json` | JSON | Small monitoring cursor (last fetch time, last recommendation time, dry-run counter) |

JSON files are pretty-printed and overwritten in full on each save. JSONL files
are append-only — one JSON object per line, never rewritten. Both use UTF-8
without BOM.

---

## `config.json`

Global settings for the pipeline running in this profile.

```json
{
  "schema_version": 2,
  "pipeline": "research_signal_profile_aware",
  "score_weights": {
    "relevance": 0.28,
    "open_question_match": 0.24,
    "novelty": 0.18,
    "source_tier": 0.12,
    "profile_role": 0.12,
    "actionability": 0.06
  },
  "threshold": 0.65,
  "source_policy": {
    "arxiv_fallback_enabled": false,
    "arxiv_fallback_note": "..."
  },
  "daily_policy": {
    "max_topics_per_day": 1,
    "default_mode": "single_research_signal",
    "allow_mini_digest_when_related_signals": true,
    "belief_updates_require_confirmation": true
  }
}
```

Fields:

- `schema_version` (int, required) — bump when incompatible fields change.
- `pipeline` (string, required) — pipeline identifier; the fetcher and scorer
  check this before running.
- `score_weights` (object, required) — weights summing to ~1.0 across scoring
  dimensions. Each weight ∈ [0, 1].
- `threshold` (float, required, ∈ [0, 1]) — minimum `final_score` for a
  candidate to be promoted to recommended.
- `source_policy.arxiv_fallback_enabled` (bool, optional, default `false`) —
  gate on raw arXiv keyword fallback. Also togglable at runtime with
  `RESEARCH_COPILOT_ARXIV_FALLBACK=1`.
- `daily_policy.max_topics_per_day` (int, optional, default `1`).
- `daily_policy.default_mode` (string, optional) — one of
  `single_research_signal | mini_digest`.
- `daily_policy.belief_updates_require_confirmation` (bool, optional,
  default `true`) — the copilot never rewrites `research_profile.json`
  automatically; proposals go through `/paper apply` (Phase 5+).

---

## `topics.json`

Named research topics that drive scoring. `topics` is a list; ordering is not
significant.

```json
{
  "topics": [
    {
      "id": "research-agent",
      "name": "Research Agent",
      "priority": 0.98,
      "status": "active",
      "description": "Deep research agents ...",
      "include": ["deep research agent", "evidence chain", "..."],
      "exclude": ["generic multi-agent", "pure RAG"],
      "open_questions": ["How should long trajectories be compressed?", "..."],
      "updated_at": "2026-07-12"
    }
  ]
}
```

Per-topic fields:

- `id` (string, required, kebab-case) — stable identifier.
- `name` (string, required) — display name shown in `/paper topics`.
- `priority` (float, required, ∈ [0, 1]) — used as a multiplier during scoring.
- `status` (string, required) — one of:
  - `active` — considered by the fetcher and the scorer.
  - `dormant` — kept in the file but skipped by pipeline runs. Reactivated
    only by user action.
- `description` (string, optional) — human context; not used for matching.
- `include` (list of strings, optional) — positive keyword/phrase signals.
- `exclude` (list of strings, optional) — negative signals; a candidate that
  matches any of these is deprioritized.
- `open_questions` (list of strings, optional) — free-text questions used to
  drive the `open_question_match` dimension in scoring.
- `updated_at` (ISO date string, required) — last edit.

---

## `research_profile.json`

The user's research stance. Read by the scorer for the `profile_role`
dimension and by the daily pick prompt for grounding. The Research Copilot
never mutates this file automatically; changes come via `/paper apply` on an
explicit proposal.

```json
{
  "schema_version": 1,
  "updated_at": "2026-07-12",
  "profile_type": "academic_view_profile",
  "positioning": "Personal research stance profile for Research Copilot.",
  "long_term_agenda": [
    {
      "id": "research-agent-long-horizon",
      "name": "Research Agent and Long-Horizon Agent Behavior",
      "priority": 0.98,
      "core_question": "How can research agents sustain reliable ... ?",
      "current_beliefs": [
        {
          "id": "trajectory-over-final-answer",
          "statement": "Long-horizon agent quality should be evaluated at ...",
          "confidence": 0.78,
          "updated_at": "2026-07-12"
        }
      ],
      "open_questions": ["..."],
      "knowledge_gaps": ["..."]
    }
  ]
}
```

Per-agenda-item fields mirror `topics.json` conventions. `current_beliefs[]`
each carry a `confidence ∈ [0, 1]` and a per-belief `updated_at`. Proposals
change belief `confidence` or add new beliefs; they never delete existing ones
without user confirmation.

---

## `source_registry.json`

Curated feeds and domains that the fetcher polls.

```json
{
  "schema_version": 1,
  "updated_at": "2026-07-12",
  "notes": "...",
  "sources": [
    {
      "id": "anthropic-news",
      "name": "Anthropic News",
      "type": "company_blog",
      "tier": 1.0,
      "url": "https://www.anthropic.com/news",
      "feed_url": "https://www.anthropic.com/news/rss.xml",
      "domains": ["anthropic.com"],
      "topics": ["research-agent", "long-horizon-agent", "..."],
      "bias_note": "Company narrative; strong signal for agent behavior."
    }
  ]
}
```

Per-source fields:

- `id` (string, required, kebab-case).
- `name` (string, required).
- `type` (string, required) — free-form label; conventional values include
  `company_blog | lab_blog | rss_aggregator | preprint_server | conference |
  newsletter`.
- `tier` (float, required, ∈ [0, 1]) — used as the `source_tier` scoring
  dimension. Higher = more trusted.
- `url` (string, required) — canonical human-readable URL.
- `feed_url` (string, optional) — RSS/Atom URL. Absent when the source is
  polled via Tavily / topic search on `domains` instead of a feed.
- `domains` (list of strings, optional) — used to constrain topic searches.
- `topics` (list of topic ids, optional) — hint about which topics this
  source tends to produce.
- `bias_note` (string, optional) — human context.

---

## `candidates.jsonl`

Every item the fetcher has seen, one JSON object per line. Grows over time;
never rewritten in place. `id` is unique across the file — new fetches
skip already-present ids.

Example (candidate that has been promoted to a recommendation):

```json
{
  "id": "2607.08768",
  "type": "paper",
  "title": "UniClawBench: A Universal Benchmark for Proactive Agents on Real-World Tasks",
  "authors": ["Zhekai Chen", "Chengqi Duan", "Kaiyue Sun", "Bohao Li", "Yuqing Wang"],
  "url": "https://arxiv.org/abs/2607.08768",
  "arxiv_id": "2607.08768",
  "summary": "The rapid development of large language models ...",
  "published": "2026-07-09",
  "discovered_at": "2026-07-12T06:00:59.973846+00:00",
  "sources": [{"name": "arxiv_api_fallback", "topic": "Research Agent"}],
  "source_tier": 0.5,
  "topics": [{"name": "Research Agent", "confidence": 0.5}],
  "status": "recommended",
  "scored": true,
  "final_score": 0.86,
  "score_dimensions": {
    "relevance": 0.88,
    "open_question_match": 0.86,
    "novelty": 1.0,
    "source_tier": 0.5,
    "profile_role": 0.9,
    "actionability": 0.78
  },
  "signal_role_candidates": ["Gap filler", "Trend signal", "Tool useful"]
}
```

Required fields: `id`, `type`, `title`, `url`, `discovered_at`, `status`.
Everything else is optional and depends on `type` and pipeline state.

**`type`** — free-form; conventional values: `paper | blog | release | post`.

**`status`** — the item's position in the pipeline:

- `candidate` — ingested but not yet scored.
- `recommended` — scored above `threshold` and delivered to the user.
- `saved` — user marked with `/paper save <id>` (still in the queue for
  reading; the interaction record is authoritative for who did it and when).
- `read` — user marked with `/paper read <id>`.
- `skipped` — user marked with `/paper skip <id> [reason]`.
- `archived` — moved out of active consideration; not surfaced by
  `/paper history` unless explicitly requested.

Status transitions are one-way in practice (a `skipped` item is not
re-promoted). The authoritative "why did this move" record is
`interactions.jsonl`, not the status field.

**`scored` / `final_score` / `score_dimensions`** — populated only after
scoring runs. `final_score ∈ [0, 1]`. Each dimension in `score_dimensions`
mirrors the keys in `config.json::score_weights`.

**`signal_role_candidates`** — free-text tags proposed by the scorer for the
daily pick prompt (see Phase 5 daily-pick format).

**`sources`** — list of `{name, topic}` records showing which feed/topic
combination first surfaced the item.

---

## `recommendations.jsonl`

The push-log for what was delivered to the user. One line per recommendation;
never rewritten. Cross-references `candidates.jsonl` by `id`.

Example (Phase 2 canonical format):

```json
{
  "id": "2607.08768",
  "recommended_at": "2026-07-12T09:02:54.221128Z",
  "score": 0.86,
  "type": "paper",
  "title": "UniClawBench: A Universal Benchmark for Proactive Agents on Real-World Tasks",
  "url": "https://arxiv.org/abs/2607.08768",
  "topic_matches": ["Research Agent", "Long-Horizon Agent"],
  "signal_roles": ["Gap filler", "Trend signal"],
  "open_questions_addressed": ["What process-level metrics best capture ..."]
}
```

Required fields: `id`, `recommended_at`, `score`, `title`. Everything else is
optional; older records may be missing `type`, `url`, or `signal_roles`.

`recommended_at` is an ISO-8601 UTC timestamp (either with `Z` or a
`+00:00` offset — both forms are read).

---

## `interactions.jsonl`

Append-only feedback log. Every `/paper save`, `/paper skip`, `/paper read`,
and `/paper feedback` call writes exactly one line here. The
`update_candidate_status()` helper also updates the corresponding
`candidates.jsonl` entry in place.

Example — a save with no additional text:

```json
{
  "kind": "save",
  "item_id": "2607.08768",
  "at": "2026-07-12T15:00:00Z",
  "profile": "default",
  "source": "weixin"
}
```

Example — a skip with a reason:

```json
{
  "kind": "skip",
  "item_id": "2607.08769",
  "at": "2026-07-12T15:01:00Z",
  "profile": "default",
  "source": "weixin",
  "reason": "too far from current agenda"
}
```

Example — freeform feedback that produces a proposal for later review:

```json
{
  "kind": "feedback",
  "item_id": "2607.08768",
  "at": "2026-07-12T15:02:00Z",
  "profile": "default",
  "source": "cli",
  "text": "This is exactly the kind of eval work I want more of.",
  "proposal_id": null
}
```

Required fields: `kind`, `at`. Everything else depends on `kind`.

> **Compatibility note:** older records written by the initial
> `research_copilot.commands` handler use `type` in place of `kind` and
> `created_at` in place of `at`. Readers (`paper_health.py`, tests) accept
> either shape; new writes should use the canonical `kind`/`at`. Do not
> rewrite historical records — the log is append-only.

**`kind`** — one of:

- `save` — user wants to read/keep this. Sets `candidates[id].status = saved`.
- `skip` — user is not interested. Sets `status = skipped`. Optional `reason`.
- `read` — user has read it. Sets `status = read`.
- `feedback` — free-text opinion attached to an item. `text` required.
- `wrong_reason` — the recommendation was scored against the wrong signal;
  used by the weekly health report to identify scoring drift. Optional
  `text` explaining what the correct signal would have been.
- `topic_adjustment_requested` — user is asking to raise/lower a topic's
  priority, or mark it dormant. Emits a proposal (Phase 5+); do not mutate
  `topics.json` on receipt.

**`source`** — one of `weixin | cli | telegram | slack | discord | api`.
Records where the interaction originated. Never contains user-identifying
information; the profile itself is the user.

**`at`** — ISO-8601 UTC timestamp.

**`profile`** — the profile name this interaction belongs to. Redundant with
the file's path (each profile has its own `interactions.jsonl`), but included
so that logs remain interpretable if concatenated across profiles.

**`proposal_id`** — reserved for the Phase 5 `/paper apply|reject` flow.
Present in `feedback` and `topic_adjustment_requested` records when a
proposal has been generated. `null` before then.

---

## `state.json`

A small monitoring cursor read by cron scripts and the `/paper health` report.
Overwritten in full on each update.

```json
{
  "last_fetch_at": "2026-07-12T10:01:44.275683+00:00",
  "last_fetch_count": 3,
  "last_recommendation_at": "2026-07-12T14:30:00Z",
  "consecutive_no_pick_days": 0
}
```

All fields optional; a fresh profile bootstrap writes `{}` here and the
first fetcher/pick run populates fields as they become known.

- `last_fetch_at` (ISO-8601 UTC) — when the fetcher last ran, regardless of
  whether it produced any new candidates.
- `last_fetch_count` (int) — number of items ingested on that run.
- `last_recommendation_at` (ISO-8601 UTC) — when the last recommendation
  was delivered.
- `consecutive_no_pick_days` (int) — days in a row that the daily-pick job
  produced no recommendation. Used to flag stale pipelines in the weekly
  health report.

---

## Allowed enum values (summary)

For quick reference by tests and code that validate these files.

**Candidate statuses:**
`candidate | recommended | saved | read | skipped | archived`

**Interaction kinds:**
`save | skip | read | feedback | wrong_reason | topic_adjustment_requested`

**Interaction sources:**
`weixin | cli | telegram | slack | discord | api`

**Topic statuses:**
`active | dormant`

**Source types (conventional, free-form):**
`company_blog | lab_blog | rss_aggregator | preprint_server | conference | newsletter`

---

## Profile isolation

Every profile's Research Copilot state is fully local to its `HERMES_HOME`.
No file listed here is ever shared across profiles. When code needs the
current profile's data directory, it must call
`research_copilot.storage.get_data_dir()`; direct `Path.home() / ".hermes"`
references break multi-profile deployments.

See `docs/research-copilot/weixin-profile-gateway.md` for the one-profile
one-gateway product boundary that this data contract sits inside.
