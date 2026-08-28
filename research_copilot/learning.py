"""Reader-facing study packages compiled from auditable Library state."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .ranking.scoring import ScoreResult


@dataclass(frozen=True)
class LearningPackage:
    item_id: str
    title: str
    url: str
    three_minute_summary: str
    why_recommended: tuple[str, ...]
    evidence_boundary: str
    recall_questions: tuple[str, ...]
    next_actions: tuple[str, ...]
    agent_analysis_status: str
    user_learning_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compact(text: str, *, max_chars: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(1, max_chars - 1)].rstrip() + "…"


def build_learning_package(
    item: Mapping[str, Any],
    ranking: ScoreResult,
    *,
    prompt_texts: Mapping[str, str] | None = None,
    max_summary_chars: int = 900,
) -> LearningPackage:
    """Build a deterministic study aid without pretending an abstract is a paper."""
    title = str(item.get("title") or "Untitled")
    summary = _compact(str(item.get("summary") or ""), max_chars=max_summary_chars)
    if not summary:
        summary = "当前条目缺少可用摘要；请先打开原文，不要据标题推断结论。"

    prompt_texts = prompt_texts or {}
    matched_prompts = (
        *ranking.matched_question_ids,
        *ranking.matched_knowledge_gap_ids,
    )
    why: list[str] = []
    if ranking.primary_topic_id:
        why.append(f"命中主主题 {ranking.primary_topic_id}")
    for prompt_id in matched_prompts[:2]:
        prompt = _compact(prompt_texts.get(prompt_id, prompt_id), max_chars=140)
        why.append(f"关联当前问题：{prompt}")
    if not why:
        why.append(f"候选排序得分 {ranking.score:.2f}，建议动作 {ranking.suggested_action}")

    evidence_boundary = (
        "本学习包仅依据 Library 中的来源元数据与摘要生成；实验数字、因果解释和局限"
        "在标记 deep_researched 前均视为待全文核验。"
    )
    questions = (
        f"{title} 试图解决的核心问题是什么？",
        "作者的关键机制或方法，与最接近的已有做法相比改变了什么？",
        "哪一项证据最能支持结论，哪一个局限最可能使结论失效？",
    )
    item_id = str(item.get("id") or ranking.item_id)
    actions = (
        f"/paper start {item_id}",
        f"/paper read {item_id}",
        f"/paper skip {item_id} <原因>",
    )
    return LearningPackage(
        item_id=item_id,
        title=title,
        url=str(item.get("url") or ""),
        three_minute_summary=summary,
        why_recommended=tuple(why),
        evidence_boundary=evidence_boundary,
        recall_questions=questions,
        next_actions=actions,
        agent_analysis_status=str(item.get("agent_analysis_status") or "none"),
        user_learning_status=str(item.get("user_learning_status") or "unseen"),
    )


def render_learning_package(package: LearningPackage, *, index: int | None = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    lines = [f"{prefix}{package.title} [{package.item_id}]", "", "3 分钟摘要：", package.three_minute_summary]
    if package.url:
        lines.extend(["", f"原文：{package.url}"])
    lines.extend(["", "为什么推荐："])
    lines.extend(f"- {reason}" for reason in package.why_recommended)
    lines.extend(["", "证据边界：", package.evidence_boundary, "", "回忆问题："])
    lines.extend(f"- {question}" for question in package.recall_questions)
    lines.extend(["", "下一步：", " · ".join(package.next_actions)])
    return "\n".join(lines)
