# Source Hierarchy & Data Schemas

## Source Tiers

| Rank | Source | Tier | Endpoint | API Key | Notes |
|------|--------|------|----------|---------|-------|
| 1 | HF Daily Papers | 1.0 | huggingface.co/api/daily_papers | None | Community upvoted, best signal |
| 2 | Newsletters | 0.90 | Gmail IMAP (ResearchFeeds label) | GMAIL_APP_PASSWORD | Human-curated email subs |
| disabled | Semantic Scholar | 0.85 | api.semanticscholar.org/graph/v1/paper/search | None | Provider retained for historical identity; keep catalog entry disabled |
| 3 | AlphaXiv | 0.85 | alphaxiv.org | None | Discussion activity |
| 4 | GitHub Trending | 0.80 | github.com/trending | None | Star velocity |
| 5 | Tavily | 0.70 | api.tavily.com/search | TAVILY_API_KEY | Gated to quality domains |
| 6 | arXiv API fallback | 0.50 | export.arxiv.org/api/query | None | Raw, noisy, broad coverage |

Tavily restricted domains: huggingface.co/papers, paperswithcode.com, semanticscholar.org, openreview.net, alphaxiv.org, blog.google, openai.com, anthropic.com, github.com.

## Runtime configuration schemas

`topics.yaml` owns discovery and classification only:

```yaml
schema_version: 2
topics:
  - id: research-agent
    name: Research Agent
    status: active
    search_queries: [agentic research]
    match_terms: [research agent]
    exclude_terms: [game agent]
```

`research-profile.yaml` owns research intent and links back to topics:

```yaml
schema_version: 2
long_term_agenda:
  - id: reliable-research-agents
    topic_ids: [research-agent]
    priority: 0.9
    core_question: How should research agents maintain reliable evidence?
    open_questions: [How should evidence-chain recovery be evaluated?]
    knowledge_gaps: []
```

Use `hermes research config validate` after edits. Use `hermes research config
migrate` to preview a v1 conversion and add `--apply` only after reviewing the
reported conflicts and backup behavior.

The active deterministic scoring formula is defined by
`research_copilot/ranking/scoring.py`. Do not reproduce or override its weights
inside a skill.

Behavioral triage and promotion limits live in profile-aware `config.yaml`:

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
```

RSS/Atom collection uses durable `ETag` and `Last-Modified` validators. Repeated
failures enter exponential cooldown; inspect it with `hermes research health`
or `hermes research doctor`. A dry run deliberately bypasses both cooldown and
stored validators, performs a full diagnostic fetch, and writes no runtime
state. Semantic Scholar catalog entries should remain disabled.

Gmail collection uses `(mailbox, UIDVALIDITY, last_uid)` as its incremental
cursor. UIDVALIDITY changes trigger a bounded lookback instead of trusting a
stale UID. Never advance past a failed or partially emitted newsletter issue.

RSS/Atom parsing must inspect namespaced full-content and provenance fields,
not only `<description>`. Use a bounded dry run with `--show-items` to review
`no_topic_match` and `excluded:*` samples. A parser that sees entries but emits
none is `parser_degraded`; a parser that emits valid items later rejected by
topic policy is `overfiltered` instead.

Use `hermes research triage` for the cheap candidate layer. Only an item that
survives triage should proceed to recommendation/deep reading. Secondary topic
matches provide a bounded bonus; they never sum their full priorities.

## Legacy JSON schemas

The following files are migration inputs only. Current workflows must not read
or update them.

### topics.json
```json
{
  "topics": [{"id": "research-agent", "name": "Research Agent", "priority": 0.95, "status": "active|dormant", "description": "", "include": ["keyword1"], "exclude": ["keyword"], "open_questions": ["Q1?"], "updated_at": "ISO8601"}]
}
```

### candidates.jsonl
```json
{"id": "arxiv_id or uuid", "type": "paper|project", "title": "", "authors": [], "url": "", "arxiv_id": "", "summary": "", "published": "YYYY-MM-DD", "discovered_at": "ISO8601", "sources": [{"name": "hf_daily", "topic": "Research Agent"}], "source_tier": 1.0, "topics": [{"name": "Research Agent", "confidence": 0.5}], "status": "candidate|recommended|passed", "scored": false}
```

Source name values: hf_daily, semantic_scholar, alphaxiv, github_trending, tavily, newsletter, arxiv_api_fallback.

### recommendations.jsonl
```json
{"id": "rec_YYYYMMDD_NNN", "item_id": "arxiv_id", "title": "", "recommended_at": "ISO8601", "score": 0.87, "reason": "brief explanation", "delivery": {"channel": "weixin", "status": "sent"}}
```

### interactions.jsonl
```json
{"recommendation_id": "rec_...", "action": "saved|read|skip|wrong-reason|more-like-this", "timestamp": "ISO8601"}
```

### state.json
```json
{"last_fetch_at": "ISO8601", "last_fetch_count": 32, "last_recommendation_at": "ISO8601|null", "consecutive_no_pick_days": 0}
```

## Delivery Schedule

| Cron | Frequency | Type |
|------|-----------|------|
| paper-fetcher | 6:00 / 18:00 daily | no_agent=True |
| daily-paper-pick | 8:30 daily | agent-run, skill=paper |
| paper-health-report | Sat 9:00 | no_agent=True, only when consecutive_no_pick_days>=3 |

## Network Connectivity (GFW-restricted network)

| Source | Status | Workaround |
|--------|--------|-----------|
| GitHub Trending | ✅ Reachable | |
| Gmail IMAP | ✅ Reachable | Port 993 SSL only; port 143 blocked |
| arXiv API | ✅ Reachable | export.arxiv.org |
| Tavily API | ✅ Reachable | api.tavily.com |
| Semantic Scholar API | Disabled | Keep the Source Catalog entry disabled |
| Hugging Face | ✗ Blocked (GFW) | Route through local Mihomo proxy (127.0.0.1:7890) |
| AlphaXiv | ✗ Blocked | Route through same Mihomo proxy |

The fetch script probes both paths: blocked sources fall through to `_fetch_via_proxy()` which uses `urllib.request.ProxyHandler` pointed at the Mihomo proxy. Direct sources keep working without proxy. Failure is graceful — proxy errors are logged but the pipeline continues with reachable sources.

## Design Principles

1. Community signal > algorithmic discovery. A paper on HF Daily Papers is worth more than ten raw arXiv matches.
2. Open questions > keywords. Keywords are for recall; open questions are for relevance.
3. Recommended != Read. Separate state machines.
4. Better silent than spam. No candidate above 0.65 = no message.
5. Source tier weights, not filters. A Tavily paper (0.7) with high relevance/novelty can beat an HF paper (1.0).
6. Distinguish claims from interpretation in every brief. Always use "The authors claim" for paper statements.
