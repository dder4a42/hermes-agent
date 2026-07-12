# Profile-aware Research Copilot Design Notes

Session learning: Research Copilot should operate as a research signal filter, not only as a paper recommender.

## User research center of gravity

Core topics:
- Research agent / deep research agent
- Multimodal LMM, GUI agent, computer-use agent
- Long-horizon agent failure modes
- Evaluation harness and trajectory-level evaluation
- Agent infra: memory, context, tool-use, monitoring
- RL post-training for agent behavior

Adjacent only:
- World model. Recommend only when it connects to multimodal agents, long-horizon prediction, environment modeling, GUI/physical interaction, or action-conditioned dynamics. Do not treat world model as a core expertise area.

## Pipeline concept

1. Source monitoring collects ResearchSignal candidates, not only papers.
2. Academic profile alignment maps candidates to the user’s long-term agenda, beliefs, open questions, and knowledge gaps.
3. Daily output chooses one topic/signal and explains why it deserves attention today.

## ResearchSignal types

Normalize papers, lab blogs, company technical posts, researcher blogs, project pages, repos, benchmarks, datasets, model releases, system cards, discussions, talk slides, and newsletters into one candidate pool.

## Candidate roles

Every recommended signal should be classified as one or more:
- Evidence update: supports or sharpens an existing belief
- Belief challenge: challenges an existing belief
- Gap filler: fills a known knowledge gap
- Trend signal: multiple sources point to the same direction
- Tool useful: directly useful for experiments, engineering, monitoring, or evaluation

## Source strategy

Paper-only sources are too slow for frontier trend tracking. Add high-signal community and institutional sources:
- Frontier lab/company blogs: OpenAI, Anthropic, DeepMind, Google Research, Meta AI, NVIDIA, Microsoft Research, AI2
- Academic/researcher blogs: BAIR, Stanford HAI/CRFM, Interconnects
- Community/newsletter sources: Latent Space, LessWrong/Alignment Forum, AlphaXiv, HF Daily Papers, GitHub repos/releases

Company blogs are fast but biased; mark them as lab/product narrative, not peer-reviewed evidence.

## Output logic

The brief should answer:
1. What is today’s research signal?
2. Why does it matter to the user’s current research map?
3. Which belief/open question/gap does it touch?
4. What does the source claim?
5. What is the assistant’s interpretation and skepticism?
6. What should the user read or try next?

Use Chinese as the main language, preserve standard English technical terms, and use WeChat Markdown-lite formatting.

## Workflow pitfall

Do not start editing scripts or skills before the design is explicit. For Research Copilot changes, first write or update the design note/spec, confirm data flow and output logic, then implement.