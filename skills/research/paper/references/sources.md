# Source Hierarchy & Data Schemas

## Source Tiers

| Rank | Source | Tier | Endpoint | API Key | Notes |
|------|--------|------|----------|---------|-------|
| 1 | HF Daily Papers | 1.0 | huggingface.co/api/daily_papers | None | Community upvoted, best signal |
| 2 | Newsletters | 0.90 | Gmail IMAP (ResearchFeeds label) | GMAIL_APP_PASSWORD | Human-curated email subs |
| 3 | Semantic Scholar | 0.85 | api.semanticscholar.org/graph/v1/paper/search | None | Citation-indexed |
| 3 | AlphaXiv | 0.85 | alphaxiv.org | None | Discussion activity |
| 4 | GitHub Trending | 0.80 | github.com/trending | None | Star velocity |
| 5 | Tavily | 0.70 | api.tavily.com/search | TAVILY_API_KEY | Gated to quality domains |
| 6 | arXiv API fallback | 0.50 | export.arxiv.org/api/query | None | Raw, noisy, broad coverage |

Tavily restricted domains: huggingface.co/papers, paperswithcode.com, semanticscholar.org, openreview.net, alphaxiv.org, blog.google, openai.com, anthropic.com, github.com.

## Scoring Formula

```
final = 0.35 * relevance + 0.25 * source_tier + 0.25 * open_question_match + 0.15 * novelty
```

Each dimension 0.0–1.0. Threshold: 0.65.

When source_tier is missing from a candidate (legacy data), default to 0.5.

**⚠ Runtime override:** The formula above is the historical default. The active scoring weights are always read from `~/.hermes/research-copilot/config.json` at the start of each daily run. If config.json omits a dimension (e.g. no `source_tier` in current config), use only the dimensions present — the config is authoritative.

Current config.json weights (as of July 2026): relevance=0.4, novelty=0.3, open_question_match=0.3 (no source_tier — all arXiv candidates share tier 0.5, making it non-discriminative at the scoring stage).

## Data File Schemas

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
| Semantic Scholar API | ⚠ Rate-limited | 429 under burst; retry after backoff |
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
