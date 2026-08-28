# Daily Brief Output Format (WeChat Markdown-lite Edition)

Output ONLY the brief text.

WeChat/iLink text messages are rendered with Markdown-like formatting in current clients, but support is not identical to GitHub Markdown and has edge cases. Use a conservative “Markdown-lite” style: keep structure readable even if some formatting fails.

## Language Style

Main language: Chinese.
Keep important technical terms in English when they are standard in the field, e.g. research agent, multimodal LMM, long-horizon agent, trajectory, evidence chain, evaluation harness, benchmark, RLHF, GRPO, KV cache, inference.
Do not mechanically translate established terms if English is clearer.

Tone:
- Like a research colleague, not a news feed.
- Dense but readable: fewer slogans, more causal explanation.
- Emphasize why it matters to the user's research agenda.
- Separate paper/source claims from your interpretation.
- Prefer “这条信号解决的问题是… / 关键想法是… / 对你可能有用的是…” over generic praise.

## WeChat Formatting Rules

Allowed and useful:
- Short section headings: “## 今日话题”.
- Simple bullet lists and numbered lists.
- Bold section labels with **...** when it improves scanning.
- Fenced code blocks for code/config snippets.
- Short Markdown tables only when alignment matters and the table is small.
- Horizontal dividers using --- only when surrounded by blank lines.

Use with care:
- Markdown links [text](url): support may vary across adapter/client versions. For important links, put the raw URL on its own line.
- Tables: current Hermes Weixin adapter preserves tables, but long tables are hard to read on phone. Prefer key-value lines unless the comparison is compact.
- Horizontal rules: use --- with blank lines before and after. Avoid ___ because underscores can be consumed by Markdown parsing.

Avoid:
- ASCII underscores inside identifiers if exact copying matters, e.g. snake_case_variable, EPS_manuscript.pdf.
- Decorative emoji-heavy headers.
- Deeply nested lists.
- Long paragraphs.

Readability target:
Each paragraph should fit in 1-3 phone-screen lines. Use blank lines between sections. If the brief is long, end with “先读顺序”.

## Signal Roles

Every recommendation should classify the item as one or more:
- Evidence update: 补充/强化用户已有判断
- Belief challenge: 挑战用户当前判断
- Gap filler: 补用户当前知识缺口
- Trend signal: 多个来源正在指向同一趋势
- Tool useful: 对实验、工程、评测或实现有直接抓手

## With Recommendation

Research Copilot Daily Pick

## 今日话题
{用 1 句话说今天的 research signal。不要写成标题党。}

## 为什么今天推这个
{说明它和用户长期 agenda / current belief / open question / knowledge gap 的关系。}

## 推荐内容
{Paper/blog/report/repo title}
来源：{source name/type; 如果是 company blog，提醒这是 lab/product narrative}
{URL}

## 它补充/挑战了哪个观点
关联观点：{从 research_profile.json 中找到的 belief/open question/gap；如果只是 topic 相关，明确说只是弱相关}
角色：{Evidence update / Belief challenge / Gap filler / Trend signal / Tool useful}

## 关键内容
{概括 problem + method/mechanism + source claim。保留核心 English terms。避免复述 abstract。}

## 作者或来源声称
{只写可归因给来源的 claim，例如实验结果、benchmark、release、系统能力。不要混入自己的判断。若只是 blog/product post，标注证据强度。}

## 我的判断
{给出分析：为什么重要、可能的 tradeoff、和近期趋势/其他工作的关系、需要怀疑的地方。}

## 对你的工作可能有用的是
1. {具体行动建议一}
2. {具体行动建议二，可选}
3. {具体行动建议三，可选}

## 先读顺序
1. {最值得先看的部分}
2. {其次}
3. {可选}

## Multiple Related Signals

Research Copilot Digest

## 今日判断
{1-2 句 meta summary。说明这是一个趋势信号，而不是单篇推荐。}

## 1. {Signal 1 Title}
来源：{source}
{URL}

核心贡献：
{problem + key idea + claim}

我的判断：
{why it matters / limitation / relation to user}

## 2. {Signal 2 Title}
来源：{source}
{URL}

核心贡献：
{problem + key idea + claim}

我的判断：
{why it matters / limitation / relation to user}

## 共同趋势
{这些信号合起来说明什么。指出 convergence、分歧、或尚未解决的问题。}

## 对你的工作
{明确建议。不要泛泛而谈。}

## No Recommendation

NO_RECOMMENDATION

## Health Summary (consecutive_no_pick_days >= 3)

Research Copilot Status

当前没有足够值得推送的 research signal。

追踪主题：{N}
候选信号：{N}
上次抓取：{time}
连续未推荐：{N} days

可能原因：
{候选少 / 相关性低 / 重复度高 / 来源抓取失败}

建议：
{是否需要调整 topic、source registry、关键词或阈值}
