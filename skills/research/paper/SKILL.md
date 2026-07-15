---
name: paper
description: "Community-curated paper recommendation, one per day."
version: 1.2.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    category: research
    tags: [paper, research, recommendation, copilot, daily-brief]
---

# Paper Skill — Research Copilot

Daily personalized research paper recommendation for WeChat delivery.

**Core philosophy:** One research topic/signal per day. Community-vetted sources plus curated frontier lab/company/researcher sources; raw arXiv is fallback only. Each pick comes with a personalized brief explaining why it matters *to you* — connecting the signal to your long-term academic profile, current beliefs, open questions, and knowledge gaps.

This skill is loaded by the `daily-paper-pick` cron job (daily at 8:30am).

## Architecture

Two-tier hybrid pipeline:

paper-fetcher (no_agent=True, twice daily)
  → reads topics.json + source_registry.json + research_profile.json
  → fetches from HF Daily Papers, Semantic Scholar, AlphaXiv, GitHub Trending, newsletters, curated RSS/Atom feeds, Tavily over curated domains, and arXiv fallback
  → normalizes papers/blogs/reports/repos/discussions into ResearchSignal candidates, deduplicates, appends to candidates.jsonl
  → no LLM cost, runs silently

daily-paper-pick (agent-run, daily 8:30, loads this skill)
  → reads candidates.jsonl + topics.json + research_profile.json + source_registry.json + recommendations.jsonl
  → scores candidates and assigns a signal role (Evidence update / Belief challenge / Gap filler / Trend signal / Tool useful)
  → picks best >= threshold or stays silent
  → generates personalized brief, delivers to WeChat

## Source Tiers

| Tier | Source | Score | Why |
|------|--------|-------|-----|
| 1 | HF Daily Papers | 1.0 | Community upvoted, human-curated |
| 2 | Newsletters | 0.90 | Human-curated email subscriptions |
| 3 | Semantic Scholar | 0.85 | Citation-indexed |
| 3 | AlphaXiv | 0.85 | Discussion signal |
| 4 | GitHub Trending | 0.80 | Star adoption |
| 5 | Tavily | 0.70 | Gated to quality domains |
| 6 | arXiv API fallback | 0.50 | Raw arXiv, noisy, broad coverage |

HF Daily Papers is often GFW blocked from CN networks — use newsletter subscription. arXiv fallback exists when better sources produce no results.

## Scoring

Each candidate gets 0.0-1.0 on weighted dimensions. Default weights (see `references/sources.md`):

- relevance (0.35): match with topic keywords
- source_tier (0.25): from candidate record
- open_question_match (0.25): connects to user's open questions
- novelty (0.15): not recommended in last 30 days

**However, the runtime config.json (`${HERMES_HOME}/research-copilot/config.json`) is AUTHORITATIVE.** Its `score_weights` override the defaults above. Current config.json is profile-aware and includes relevance, open_question_match, novelty, source_tier, profile_role, and actionability. Always read config.json at the start of scoring and use its weights and threshold.

## Data Files

**⚠ PROFILE ISOLATION**: All paths below use ${HERMES_HOME}/, NOT ~/.hermes/.
The current Hermes profile is set by the HERMES_HOME environment variable
(default: ~/.hermes; when running as \`hermes -p <profile>\`, it points at
~/.hermes/profiles/<profile>/). NEVER read/write ~/.hermes/... directly — that
resolves to the OWNER's home and leaks into the default profile even when the
agent is invoked under a different profile. If read_file/write_file receives a
\`~\` prefix it will NOT be re-scoped.

All under ${HERMES_HOME}/research-copilot/:
- topics.json — interests with open questions
- source_registry.json — curated lab/company/researcher/community sources for RSS and directed search
- research_profile.json — long-term academic stance profile: agenda, beliefs, open questions, knowledge gaps, evidence ledger
- candidates.jsonl — discovered ResearchSignal items (papers, blogs, reports, repos, benchmarks, discussions)
- recommendations.jsonl — delivery history
- interactions.jsonl — user feedback
- state.json — cursors and health
- config.json — weights and threshold

## Procedure (agent cron)

**Use the ``paper`` toolset. Do NOT manually read candidates.jsonl or
config.json — Python has already scored every candidate against your
config weights and filtered anything recommended in the last 30 days.**

1. Call ``paper_top_candidates(k=20)``. Response includes:
    * ``threshold`` (from config.json) and ``weights`` (score dimensions).
    * ``items``: ranked list, each with ``{score, dims, reasons,
      candidate: {id, title, authors, url, arxiv_id, published,
      summary, sources, topics}}``. Summary is truncated to 400 chars.
    * ``total_ranked``: how many candidates cleared the novelty
      filter (before threshold).
2. Inspect ``items[0].score``:
    * If ``items`` is empty OR the top score is below ``threshold``,
      respond with exactly ``[SILENT]`` — nothing to report.
    * Otherwise proceed.
3. (Optional) Call ``paper_recent_recommendations(days=30)`` as a
   sanity check that nothing already-pushed appears in your candidate
   analysis narrative.
4. Read ``${HERMES_HOME}/skills/research/paper/references/format.md``
   for the brief style contract, and — if you need profile
   references beyond the ``reasons`` string — ``${HERMES_HOME}/research-copilot/research_profile.json``.
5. Semantic judgment: usually pick ``items[0]``. Prefer a lower-ranked
   item ONLY when there is a clear reason: (a) two adjacent items are
   near-duplicates on the same technique and a newer one exists, or
   (b) the top item is very off-profile despite scoring well. State
   the reason briefly in the brief.
6. Write the brief per references/format.md. Rules:
    * Chinese narrative, English technical terms kept.
    * Separate "authors claim" from "your analysis / meta-trend".
    * Reference specific ``open_questions`` / ``current_beliefs`` from
      the profile — quote the exact wording so the user can spot
      hallucinated references. Do NOT invent history.
7. Call ``paper_write_recommendation(item_id=<the id you picked>,
   brief_text=<your full brief>, signal_roles=[...], score=<items[i].score>)``.
   Python validates the id exists in candidates.jsonl, is still
   ``status=candidate``, and has not been recommended in the last 30
   days; then appends to recommendations.jsonl, flips
   candidates.jsonl status → ``recommended``, and updates state.json.
   If the response is ``{"success": false}``, DO NOT retry with a
   different brief — the failure is a data-state issue, not a
   phrasing one.
8. Output ONLY the brief text (it will be delivered to WeChat).
   Never output the tool responses.

**Do not read candidates.jsonl / recommendations.jsonl / config.json
directly with read_file. Everything you need is already in
paper_top_candidates response.**

### Fallback (only if paper_* tools are NOT available)

If the tools throw ``tool not found`` (agent lacks the paper toolset),
fall back to the manual procedure — read topics.json, research_
profile.json, config.json, candidates.jsonl, recommendations.jsonl,
compute scores per config.json weights, and write recommendations.
jsonl yourself. This path is error-prone (LLM manually summing
weighted floats over hundreds of candidates); prefer to fix the
enabled_toolsets on the cron job.

See `references/gfw-setup.md` for CN-network proxy config, Gmail IMAP newsletter reader, and source connectivity table.

See `references/profile-aware-research-copilot.md` for the profile-aware ResearchSignal design: academic view profile, source registry, signal roles, and the user’s corrected topic priorities.

See `references/research-signal-pipeline.md` for the latest implementation-oriented notes: source-registry strategy, arXiv fallback safety valve, WeChat Markdown-lite output contract, persistence expectations, and workflow pitfalls.

See `references/world-model-inference-optimization.md` for the user's World Model × KV cache / inference optimization interest, including TempCache, Forcing-KV, FlowCache, SANA-Video, search terms, and WeChat formatting lessons.

See `references/paper-feedback-loop.md` for the `/paper` CLI/Weixin feedback loop: profile-safe storage helpers, command set, interaction records, Weixin output style, and TDD/testing pattern.

See `references/weixin-profile-research-copilot.md` for the multi-user deployment pattern: one personal Weixin bot = one Hermes profile = one gateway process, with profile-local cron/memory/research state.

## Pitfalls

- Recommended is NOT read. Separate state machines.
- Do NOT lower the 0.65 threshold. Skip > noise.
- Do NOT recommend the same paper twice (check item_id).
- Brief MUST distinguish "authors claim" from "your analysis".
- **Language style:** Write the main narrative in Chinese, but keep standard English technical terms where clearer (e.g. world model, KV cache, RLHF, GRPO, benchmark, harness, trajectory, inference). Do not over-translate established terms.
- **Brief format: insight over listing.** A digest is not a list of links. The user explicitly rejected a too-simple World Model/KV digest as lacking insight. Each paper needs analysis: what problem it solves, why it matters to the user's specific work or the intended recipient, how it connects to other papers, and what a practitioner should do next. Always include a meta-trend paragraph and actionable advice.
- **WeChat reading experience:** Current WeChat/iLink clients render Markdown-like formatting. Use conservative Markdown-lite: short headings, simple numbered/bullet lists, occasional bold labels, code fences when needed, and small tables only when useful. Keep raw URLs on their own lines.
- **WeChat Markdown pitfalls:** ASCII underscores in filenames/identifiers can be consumed as italic markers; avoid relying on `_` display fidelity. Use `---` dividers only with blank lines around them; avoid `___`. Do not use deeply nested lists or long tables on phone.
- **World Model topic matching.** If the user asks about world model inference optimization, do NOT use broad `world model physical` keyword matching alone — it can pull unrelated quantum/physical-science papers. Use targeted terms from `references/world-model-inference-optimization.md` such as autoregressive video diffusion, KV cache compression, temporal cache, sparse attention, linear attention, SSM, and long-context video world models.
- **Raw arXiv fallback safety valve.** Raw arXiv fallback is noisy for this user's profile-aware pipeline and should stay disabled by default unless explicitly requested or stricter filtering is implemented. Prefer curated sources, source_registry feeds, newsletters, community signals, and Tavily over curated domains. If raw arXiv is re-enabled for exploration, validate results before appending to the production candidate pool.
- **Dry-run before production writes.** For fetcher/source-registry changes, run with a temporary `HERMES_HOME` before touching production `candidates.jsonl`. If approval/tooling blocks dry-run validation, report the validation gap explicitly.
- GitHub repos are projects, not papers — note type.
- Source tier is a weight, not a hard filter. Let the formula decide.
- **WeChat delivery is Markdown-lite, not full GitHub Markdown.** Preserve readability even if some formatting fails. Test delivery with a simple message before sending complex formatted content.
- **Cron schedule format.** Natural language ("every 8am", "daily at 8", "in 1m") is NOT accepted. Use 5-field cron ("0 8 * * *") or duration ("30m", "2h") or ISO timestamp ("2026-07-12T15:00:00").
- **Design before code.** Before implementing complex features (pipelines, storage, multi-step logic), stop and discuss architecture with the user first. Surface open questions, data format choices, failure modes, and tradeoffs. For Research Copilot specifically: collect source/rendering/workflow evidence, write or update the design note, confirm output logic, then implement. Do not eagerly patch templates/scripts before design details are clear.
- **Cron job tool constraints.** The daily-paper-pick runs as a cron job with no user present. In cron mode, execute_code and terminal are BLOCKED. Only read_file, write_file, patch, web_search, web_extract, browser_*, memory, skill_*, and todo are available. Scoring must be done manually in your response — read each candidate, reason through dimension scores, track the best.
- **Config.json is the source of truth for scoring weights**, NOT the SKILL.md defaults. Always read config.json at runtime and use those weights and threshold.
