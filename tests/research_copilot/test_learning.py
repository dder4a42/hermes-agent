from research_copilot.learning import build_learning_package
from research_copilot.ranking import ScoreResult


def test_learning_package_is_explicit_about_abstract_only_evidence():
    package = build_learning_package(
        {
            "id": "ri_1",
            "title": "Reliable Research Agents",
            "summary": "A compact method for evidence-grounded research agents.",
            "url": "https://example.com/paper",
            "agent_analysis_status": "triaged",
            "user_learning_status": "unseen",
        },
        ScoreResult(
            item_id="ri_1", score=0.8, dimensions={}, reasons=(),
            primary_topic_id="search-agent", matched_question_ids=("q-1",),
            suggested_action="deep-read",
        ),
        prompt_texts={"q-1": "How should research agents verify claims?"},
    )

    assert package.agent_analysis_status == "triaged"
    assert package.user_learning_status == "unseen"
    assert "元数据与摘要" in package.evidence_boundary
    assert "全文核验" in package.evidence_boundary
    assert package.next_actions[0] == "/paper start ri_1"
