from datetime import datetime, timezone
from pathlib import Path

from research_copilot.library import (
    LibraryRepository,
    ResearchItemDraft,
    SourceEvidence,
    connect_library,
    initialize_library,
)
from research_copilot.wiki import audit_wiki, reconcile_wiki_analysis, render_audit


def test_audit_resolves_aliases_and_reports_invariants(tmp_path: Path) -> None:
    root = tmp_path / "Research Library"
    papers = root / "01 - Papers"
    concepts = root / "concepts"
    papers.mkdir(parents=True)
    concepts.mkdir()
    paper = papers / "2026-08-08 - paper.md"
    paper.write_text(
        "---\ntype: paper\ntitle: Paper Title\naliases: [Paper Title]\n"
        "arxiv_id: '2608.00001'\n---\n# Paper Title\n\n"
        "## 解读\n\n**背景**：filled\n\n## 相关笔记\n\n[[concept]]\n",
        encoding="utf-8",
    )
    concept = concepts / "concept.md"
    concept.write_text(
        "---\ntype: concept\ntitle: Concept\ncreated: 2026-08-08\nupdated: 2026-08-08\n"
        "tags: [test]\nsources: [2608.00001]\n---\n[[Paper Title]] and [[missing-page]]\n",
        encoding="utf-8",
    )
    (root / "00 - Index.md").write_text(
        "# Index\n\n- [[2026-08-08 - paper|Paper Title]]\n- [[concept]]\n",
        encoding="utf-8",
    )

    audit = audit_wiki(root)
    assert any(issue.code == "broken_link" and "missing-page" in issue.message for issue in audit.issues)
    assert not any(issue.code == "missing_from_index" for issue in audit.issues)
    assert "errors=" in render_audit(audit)
    assert '"issues"' in render_audit(audit, as_json=True)


def test_audit_detects_duplicate_identity_and_unindexed_page(tmp_path: Path) -> None:
    root = tmp_path / "Research Library"
    papers = root / "01 - Papers"
    papers.mkdir(parents=True)
    for name in ("one.md", "two.md"):
        (papers / name).write_text(
            "---\ntype: paper\ntitle: Duplicate\narxiv_id: '2608.00001'\n---\n# Duplicate\n",
            encoding="utf-8",
        )
    (root / "00 - Index.md").write_text("# Index\n\n- [[one]]\n", encoding="utf-8")

    codes = [issue.code for issue in audit_wiki(root).issues]
    assert "duplicate_paper" in codes
    assert "missing_from_index" in codes


def test_audit_counts_analysis_written_on_following_lines(tmp_path: Path) -> None:
    root = tmp_path / "Research Library"
    papers = root / "01 - Papers"
    papers.mkdir(parents=True)
    (papers / "paper.md").write_text(
        "---\ntype: paper\ntitle: Paper\naliases: [Paper]\n---\n"
        "# Paper\n\n## 解读\n\n**背景**：\n多行正文。\n\n"
        "**现象**：\n第二段。\n\n## 相关笔记\n",
        encoding="utf-8",
    )
    (root / "00 - Index.md").write_text("# Index\n\n- [[paper]]\n", encoding="utf-8")

    codes = [issue.code for issue in audit_wiki(root).issues]
    assert "analysis_empty" not in codes


def test_reconcile_promotes_agent_analysis_without_changing_learning_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at=now.isoformat())
    repository = LibraryRepository(connection)
    repository.upsert_source(
        source_id="test", provider="test", display_name="Test",
        source_type="paper", tier=1.0, now=now,
    )
    result = repository.upsert_item(
        ResearchItemDraft(title="Analyzed Paper", arxiv_id="2608.00001"),
        source=SourceEvidence("test"), discovered_at=now,
    )
    root = tmp_path / "Research Library"
    papers = root / "01 - Papers"
    papers.mkdir(parents=True)
    (papers / "paper.md").write_text(
        "---\ntype: paper\ntitle: Analyzed Paper\narxiv_id: '2608.00001'\n---\n"
        "## 解读\n\n**背景**：\nA\n\n**现象**：\nB\n\n**观点**：\nC\n\n"
        "**方法**：\n\n**实验设计**：\n\n**观察结论**：\n\n**局限**：\n",
        encoding="utf-8",
    )

    preview = reconcile_wiki_analysis(root, repository, apply=False, updated_at=now)
    assert preview.candidates == (result.item_id,)
    assert connection.execute(
        "SELECT agent_analysis_status FROM research_items WHERE id=?", (result.item_id,),
    ).fetchone()[0] == "none"

    applied = reconcile_wiki_analysis(root, repository, apply=True, updated_at=now)
    assert applied.updated == (result.item_id,)
    row = connection.execute(
        "SELECT agent_analysis_status,user_learning_status FROM research_items WHERE id=?",
        (result.item_id,),
    ).fetchone()
    assert tuple(row) == ("deep_researched", "unseen")
    connection.close()
