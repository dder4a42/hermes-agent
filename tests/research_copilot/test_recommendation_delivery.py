from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone


def _write_config(data):
    (data / "topics.yaml").write_text("""
topics:
  - id: reliable-agents
    name: 可靠智能体
    status: active
    priority: 0.95
    description: 长程任务中的可靠性与错误恢复。
    include: [agent reliability]
    open_questions:
      - 如何评估轨迹中的错误恢复？
""", encoding="utf-8")
    (data / "research-profile.yaml").write_text("""
positioning: 关注长程研究智能体的可靠性
long_term_agenda:
  - id: reliable-agents
    name: 可靠研究智能体
    current_beliefs:
      - id: trajectory-over-answer
        statement: 评估应关注完整轨迹而不只是最终答案。
        confidence: 0.8
    open_questions:
      - 如何评估轨迹中的错误恢复？
    knowledge_gaps:
      - 缺少真实长程任务的失效分析。
""", encoding="utf-8")
    (data / "sources.yaml").write_text("schema_version: 1\nsources: []\n", encoding="utf-8")


def _seed(data):
    from research_copilot.library import ResearchItemDraft, SourceEvidence, TopicMatch
    from research_copilot.runtime import open_library

    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    connection, repository = open_library(data / "library.db")
    repository.upsert_source(
        source_id="test", provider="rss", display_name="Test", source_type="feed",
        tier=.9, now=now,
    )
    result = repository.upsert_item(
        ResearchItemDraft(
            title="Reliable Agents Recover from Trajectory Errors",
            summary="We study recovery behavior in long-horizon agent trajectories.",
            url="https://example.com/paper", item_type="paper",
            published_at="2026-07-29T00:00:00+00:00",
        ),
        source=SourceEvidence("test"),
        topics=(TopicMatch("reliable-agents", .95),), discovered_at=now,
    )
    connection.close()
    return result.item_id


def test_delivery_context_contains_item_interests_and_research_viewpoint(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(data)
    item_id = _seed(data)

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    result = cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=False, threshold=0,
        delivery_context=True,
    ))

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "research_recommendation_context"
    assert payload["item"]["id"] == item_id
    assert payload["matched_topics"][0]["name"] == "可靠智能体"
    assert payload["learning_package"]["item_id"] == item_id
    assert "元数据与摘要" in payload["learning_package"]["evidence_boundary"]
    assert payload["learning_package"]["user_learning_status"] == "unseen"
    belief = payload["research_profile"]["long_term_agenda"][0]["current_beliefs"][0]
    assert "完整轨迹" in belief["statement"]

    # Until the scheduler acknowledges delivery, the exact same durable
    # payload is retried and no second recommendation is consumed.
    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=False, threshold=0,
        delivery_context=True,
    )) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried == payload
    from research_copilot.runtime import open_library
    connection, repository = open_library(data / "library.db")
    try:
        assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        pending = repository.pending_delivery()
        assert pending["attempt_count"] == 2
    finally:
        connection.close()


def test_delivery_context_is_silent_when_nothing_qualifies(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(data)

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=False, threshold=.72,
        delivery_context=True,
    )) == 0
    assert capsys.readouterr().out == ""


def test_retry_script_sends_pending_payload_without_creating_recommendation(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "profile"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(data)
    item_id = _seed(data)

    from hermes_cli.research_copilot_cmd import cmd_research_copilot
    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=False, threshold=0,
        delivery_context=True,
    )) == 0
    expected = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        "research_copilot.scripts.library_recommend.reconcile_previous_delivery",
        lambda: None,
    )
    from research_copilot.scripts.library_recommend_delivery import main
    assert main() == 0
    assert json.loads(capsys.readouterr().out) == expected

    from research_copilot.runtime import open_library
    connection, repository = open_library(data / "library.db")
    try:
        assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        assert repository.pending_delivery()["attempt_count"] == 2
        assert repository.pending_delivery()["item_id"] == item_id
    finally:
        connection.close()


def test_cron_receipt_acknowledges_attempted_outbox_delivery(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(data)
    _seed(data)

    from hermes_cli.research_copilot_cmd import cmd_research_copilot
    from research_copilot.scripts.library_recommend import reconcile_previous_delivery

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=False, threshold=0,
        delivery_context=True,
    )) == 0
    capsys.readouterr()
    monkeypatch.setattr("cron.jobs.list_jobs", lambda include_disabled=True: [{
        "name": "research-library-recommend",
        "last_run_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        "last_status": "ok",
        "last_delivery_error": None,
    }])

    assert reconcile_previous_delivery() is not None
    from research_copilot.runtime import open_library
    connection, repository = open_library(data / "library.db")
    try:
        assert repository.pending_delivery() is None
        assert connection.execute(
            "SELECT status FROM recommendation_delivery_outbox"
        ).fetchone()[0] == "delivered"
    finally:
        connection.close()


def test_triage_command_uses_real_profile_library_and_is_read_only(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_config(data)
    item_id = _seed(data)

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="triage", limit=1, threshold=0, json=True,
    )) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["item_id"] == item_id
    assert payload[0]["ranking"]["primary_topic_id"] == "reliable-agents"

    from research_copilot.runtime import open_library
    connection, _repository = open_library(data / "library.db")
    try:
        assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 0
        assert connection.execute("SELECT workflow_state FROM research_items").fetchone()[0] == "discovered"
    finally:
        connection.close()
