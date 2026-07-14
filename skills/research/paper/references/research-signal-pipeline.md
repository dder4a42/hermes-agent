# Research Copilot: Research Signal Pipeline Notes

Session-derived operating notes for the `paper` / Research Copilot skill.

## Updated positioning

The system should behave as a research signal filter, not a paper-title feed.

Daily output should usually be one topic/signal, interpreted through the user's academic profile:
- Core: research agents, multimodal LMMs, long-horizon agent failures, evaluation harnesses, agent infra, RL post-training for agent behavior.
- Adjacent: world model. Only recommend when it helps explain multimodal agents, long-horizon prediction, environment modeling, or action-conditioned dynamics.

## Data model additions

Expected files under `${HERMES_HOME}/research-copilot/`:
- `research_profile.json`: long-term agenda, current beliefs, open questions, knowledge gaps, evidence ledger.
- `source_registry.json`: curated lab/company/researcher/community sources with source type, tier, domains, feeds, topics, and bias notes.
- `config.json`: profile-aware scoring weights and source policy.

Candidate items should be treated as `ResearchSignal`, not only as papers. Types may include paper, blog_post, technical_report, project_page, repo, benchmark, dataset, model_release, system_card, discussion, talk_slides, newsletter_item.

## Source strategy

Prefer community and frontier-source signals over raw arXiv:
- HF Daily Papers, Semantic Scholar, AlphaXiv, newsletters.
- Frontier lab/company blogs: OpenAI, Anthropic, DeepMind, Google Research, Meta AI, NVIDIA, Microsoft Research, AI2.
- Researcher/newsletter/community sources: Interconnects, Latent Space, LessWrong/Alignment Forum, BAIR, Stanford HAI/CRFM.
- Tavily over curated domains can probe trends and fill gaps.

Raw arXiv fallback is noisy with keyword-only matching. Keep it disabled by default until stricter filtering is in place. Runtime override may be useful for one-off broad discovery, but do not let it swamp the daily candidate pool.

## Recommendation logic

For every candidate, reason about its role:
- Evidence update: supports an existing belief.
- Belief challenge: pushes against an existing belief.
- Gap filler: fills a declared knowledge gap.
- Trend signal: multiple sources point to the same direction.
- Tool useful: directly useful for experiments, evaluation, infra, or implementation.

Scoring should consider relevance, open_question_match, novelty, source_tier, profile_role, and actionability. Source tier should break ties; it should not override poor profile fit.

## Output logic

Use Chinese as the main narrative while preserving English technical terms.
Use WeChat Markdown-lite, not plain-text-only and not full GitHub Markdown:
- OK: `##` headings, simple lists, occasional bold labels, small tables, code fences, raw URL on its own line.
- Avoid: fragile ASCII underscores in identifiers, deeply nested lists, long tables, decorative formatting.

Structure should explain why the signal matters:
1. 今日话题
2. 为什么今天推这个
3. 推荐内容
4. 它补充/挑战了哪个观点
5. 关键内容
6. 作者或来源声称
7. 我的判断
8. 对你的工作可能有用的是
9. 先读顺序

## Workflow pitfalls

Do not rush implementation for Research Copilot changes. First confirm the source strategy, profile schema, scoring dimensions, and output contract. Then write a design doc and only then modify scripts/skills.

Before touching production `candidates.jsonl`, run a dry-run with temporary `HERMES_HOME` when possible. If tool approval blocks a dry-run, state that validation is incomplete rather than implying the pipeline is fully verified.

When manually producing a recommendation outside cron, persist it to `recommendations.jsonl` and mark the candidate as recommended/scored if the user wants it counted; otherwise it may be recommended again.
