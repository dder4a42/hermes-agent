from research_copilot.sources.topic_matching import match_topic_content, significant_tokens


def test_topic_match_tolerates_hyphens_word_order_and_plural_inflection():
    result = match_topic_content(
        "Agents learn from long tool-use trajectories with explicit planning.",
        include_terms=("tool use trajectory", "agent planning"),
    )
    assert result.accepted is True
    assert result.hits == ("tool use trajectory", "agent planning")


def test_topic_match_requires_all_significant_words_and_honors_excludes():
    partial = match_topic_content(
        "A tool benchmark without an agent trajectory.",
        include_terms=("tool use trajectory",),
    )
    excluded = match_topic_content(
        "On-policy distillation for image generation compression.",
        include_terms=("on-policy distillation",),
        exclude_terms=("image-generation compression",),
    )
    assert partial.accepted is False
    assert excluded.accepted is False
    assert excluded.excluded_by == ("image-generation compression",)


def test_significant_tokens_normalizes_common_plurals():
    assert significant_tokens("policies trajectories processes") == {
        "policy", "trajectory", "process",
    }


def test_fallback_tokens_must_be_in_one_local_evidence_window():
    result = match_topic_content(
        "Search is discussed in the introduction. " + "unrelated evidence " * 20 + "Agents appear in references.",
        include_terms=("search agent",),
    )
    assert result.accepted is False
