import json
from pathlib import Path

from hermes_constants import get_hermes_home


def test_ensure_data_dir_uses_active_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from research_copilot.storage import ensure_data_dir

    data_dir = ensure_data_dir()

    assert data_dir == home / "research-copilot"
    assert data_dir.is_dir()
    assert get_hermes_home() == home


def test_append_interaction_writes_jsonl_under_profile_home(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from research_copilot.storage import append_interaction

    append_interaction({"type": "save", "item_id": "paper-1", "note": "useful"})

    path = home / "research-copilot" / "interactions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [{"type": "save", "item_id": "paper-1", "note": "useful"}]


def test_read_recommendations_returns_newest_first_and_limit(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    recs = [
        {"id": "old", "title": "Old Paper", "recommended_at": "2026-01-01T00:00:00Z"},
        {"id": "new", "title": "New Paper", "recommended_at": "2026-01-02T00:00:00Z"},
    ]
    (data_dir / "recommendations.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    from research_copilot.storage import read_recommendations

    assert [r["id"] for r in read_recommendations(limit=1)] == ["new"]


def test_find_recommendation_by_id(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    (data_dir / "recommendations.jsonl").write_text(
        json.dumps({"id": "paper-1", "title": "Paper One"}) + "\n"
    )

    from research_copilot.storage import find_recommendation

    assert find_recommendation("paper-1")["title"] == "Paper One"
    assert find_recommendation("missing") is None


def test_update_candidate_status_rewrites_matching_candidate_only(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    candidates = [
        {"id": "paper-1", "title": "Paper One", "status": "candidate"},
        {"id": "paper-2", "title": "Paper Two", "status": "candidate"},
    ]
    (data_dir / "candidates.jsonl").write_text("\n".join(json.dumps(c) for c in candidates) + "\n")

    from research_copilot.storage import update_candidate_status

    assert update_candidate_status("paper-1", "saved") is True
    assert update_candidate_status("missing", "saved") is False

    rows = [json.loads(line) for line in (data_dir / "candidates.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "saved"
    assert rows[1]["status"] == "candidate"
