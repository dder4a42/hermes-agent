import argparse
import json
from pathlib import Path

import yaml
import pytest


def test_research_copilot_parser_dispatches_init_profile():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")

    def handler(args):
        return args

    build_research_copilot_parser(sub, cmd_research_copilot=handler)
    ns = parser.parse_args([
        "research-copilot",
        "init-profile",
        "alice",
        "--source",
        "default",
        "--deliver",
        "weixin",
        "--create-profile",
        "--clone",
    ])

    assert ns.command == "research-copilot"
    assert ns.research_copilot_command == "init-profile"
    assert ns.profile_name == "alice"
    assert ns.source == "default"
    assert ns.deliver == "weixin"
    assert ns.create_profile is True
    assert ns.clone is True
    assert ns.func is handler


@pytest.mark.parametrize(
    ("selected", "resolved", "failed", "expected"),
    [(17, 13, 4, 0), (4, 0, 4, 1), (0, 0, 0, 0)],
)
def test_newsletter_enrich_exit_code_distinguishes_partial_from_total_failure(
    monkeypatch, selected, resolved, failed, expected,
):
    from research_copilot.enrichment import EnrichmentSummary
    from hermes_cli.research_copilot_cmd import _enrich_newsletters

    class Connection:
        def close(self):
            pass

    class Service:
        def __init__(self, connection):
            pass

        def enrich(self, **kwargs):
            return EnrichmentSummary(
                selected=selected, resolved=resolved, failed=failed,
            )

    monkeypatch.setattr(
        "hermes_cli.research_copilot_cmd._runtime",
        lambda: {"database": Path("unused.db")},
    )
    monkeypatch.setattr(
        "research_copilot.runtime.open_library",
        lambda _path: (Connection(), object()),
    )
    monkeypatch.setattr(
        "research_copilot.enrichment.NewsletterEnrichmentService", Service,
    )

    assert _enrich_newsletters(argparse.Namespace(limit=50, dry_run=False)) == expected


def test_cmd_research_copilot_init_profile_creates_profile_and_installs_cron(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    default_home = home / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    (default_home / "research-copilot").mkdir()
    (default_home / "research-copilot" / "topics.json").write_text(json.dumps({"topics": [{"id": "agent"}]}))
    (default_home / "research-copilot" / "config.json").write_text(json.dumps({"pipeline": "verify"}))
    (default_home / "scripts").mkdir()
    (default_home / "scripts" / "paper-fetch.py").write_text("print('fetch')\n")
    (default_home / "scripts" / "paper-health.py").write_text("print('health')\n")

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    args = argparse.Namespace(
        research_copilot_command="init-profile",
        profile_name="alice",
        source="default",
        source_home=None,
        deliver="weixin",
        create_profile=True,
        clone=False,
    )

    assert cmd_research_copilot(args) == 0

    profile_home = default_home / "profiles" / "alice"
    assert (profile_home / "research-copilot" / "topics.json").exists()
    assert json.loads((profile_home / "research-copilot" / "config.json").read_text())["pipeline"] == "verify"
    assert not (profile_home / "scripts" / "paper-fetch.py").exists()
    assert (profile_home / "cron" / "jobs.json").exists()

    output = capsys.readouterr().out
    assert "Research Copilot initialized for profile 'alice'" in output
    assert "hermes -p alice gateway" in output


def test_cmd_research_copilot_requires_existing_profile_without_create(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    default_home = home / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    args = argparse.Namespace(
        research_copilot_command="init-profile",
        profile_name="alice",
        source="default",
        source_home=None,
        deliver="weixin",
        create_profile=False,
        clone=False,
    )

    assert cmd_research_copilot(args) == 1
    output = capsys.readouterr().out
    assert "Profile 'alice' does not exist" in output
    assert "--create-profile" in output


def test_research_alias_dispatches_library_commands():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)
    ns = parser.parse_args([
        "research", "collect", "--source", "rss", "--dry-run",
        "--max-requests", "2", "--max-new-items", "5", "--show-items",
    ])
    assert ns.command == "research"
    assert ns.research_copilot_command == "collect"
    assert ns.source == "rss"
    assert ns.dry_run is True
    assert ns.max_requests == 2
    assert ns.max_new_items == 5
    assert ns.show_items is True


def test_research_parser_supports_source_toggle():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)
    ns = parser.parse_args(["research", "sources", "disable", "alphaxiv"])
    assert ns.research_sources_command == "disable"
    assert ns.source_id == "alphaxiv"
    option = parser.parse_args([
        "research", "sources", "set-option", "tavily", "require_title_match", "true",
    ])
    assert option.key == "require_title_match"
    assert option.value == "true"
    budget = parser.parse_args([
        "research", "sources", "set-budget", "tavily", "max_requests", "8",
    ])
    assert budget.key == "max_requests"
    assert budget.value == 8


def test_research_parser_supports_preference_removal():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)

    topics = parser.parse_args(["research", "topics", "remove", "world-model"])
    profile = parser.parse_args(["research", "profile", "remove-agenda", "world-model-adjacent"])
    assert topics.topic_id == "world-model"
    assert profile.agenda_id == "world-model-adjacent"


def test_research_parser_supports_preference_validation_and_migration():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)

    validate = parser.parse_args(["research", "config", "validate"])
    migrate = parser.parse_args(["research", "config", "migrate", "--apply"])
    assert validate.research_config_command == "validate"
    assert migrate.research_config_command == "migrate"
    assert migrate.apply is True


def test_research_parser_supports_bounded_triage():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)
    triage = parser.parse_args(["research", "triage", "--limit", "4", "--json"])
    assert triage.research_copilot_command == "triage"
    assert triage.limit == 4
    assert triage.json is True


def test_research_parser_supports_guarded_duplicate_merge():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)
    parsed = parser.parse_args([
        "research", "duplicates", "--merge", "ri_source", "--into", "ri_target",
        "--reason", "verified", "--apply",
    ])
    assert parsed.research_copilot_command == "duplicates"
    assert parsed.merge == "ri_source"
    assert parsed.into == "ri_target"
    assert parsed.apply is True


def test_research_parser_supports_evidence_review_and_profile_proposal():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)
    add = parser.parse_args([
        "research", "evidence", "add", "ri_1", "--belief", "belief-a",
        "--relation", "supports", "--claim-type", "source_claim",
        "--strength", "0.8", "--source-quality", "primary", "--claim", "Claim",
    ])
    review = parser.parse_args([
        "research", "evidence", "review", "ev_1", "--accept", "--note", "checked",
    ])
    proposal = parser.parse_args(["research", "profile-proposal", "--dry-run"])
    assert add.research_evidence_command == "add"
    assert add.strength == 0.8
    assert review.accept is True and review.reject is False
    assert proposal.dry_run is True


def test_research_parser_supports_structured_deep_research_import():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)
    parsed = parser.parse_args([
        "research", "deep-research", "import", "artifact.yaml", "--apply",
    ])
    assert parsed.research_deep_research_command == "import"
    assert parsed.path == "artifact.yaml"
    assert parsed.apply is True

    run = parser.parse_args([
        "research", "deep-research", "run", "--item-id", "ri_1", "--dry-run",
    ])
    assert run.research_deep_research_command == "run"
    assert run.item_id == "ri_1"
    assert run.dry_run is True


def test_research_parser_supports_wiki_workflow():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)

    configure = parser.parse_args(["research", "wiki", "configure", "--vault", "/notes"])
    reconcile = parser.parse_args(["research", "wiki", "reconcile", "--apply"])
    export = parser.parse_args(["research", "wiki", "export", "--full"])
    lint = parser.parse_args(["research", "wiki", "lint", "--json"])
    publish_check = parser.parse_args(["research", "wiki", "publish-check"])
    assert configure.research_wiki_command == "configure"
    assert configure.vault == "/notes"
    assert reconcile.research_wiki_command == "reconcile"
    assert reconcile.apply is True
    assert export.full is True
    assert lint.json is True
    assert publish_check.research_wiki_command == "publish-check"


def test_wiki_configure_persists_behavioral_settings(monkeypatch, capsys):
    from hermes_cli import config as config_module
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    current = {}
    saved = []
    monkeypatch.setattr(config_module, "load_config", lambda: current)
    monkeypatch.setattr(config_module, "save_config", lambda value: saved.append(value))

    result = cmd_research_copilot(argparse.Namespace(
        research_copilot_command="wiki",
        research_wiki_command="configure",
        vault="~/Notes",
        library_subdir="Research Library",
    ))
    assert result == 0
    assert saved[0]["research_copilot"]["wiki"] == {
        "vault_path": str(Path("~/Notes").expanduser()),
        "library_subdir": "Research Library",
    }
    assert "config.yaml" in capsys.readouterr().out


def test_preference_removal_updates_yaml_and_creates_backups(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (data / "topics.yaml").write_text(
        "topics:\n  - id: world-model\n  - id: agents\n", encoding="utf-8",
    )
    (data / "research-profile.yaml").write_text(
        "long_term_agenda:\n  - id: world-model-adjacent\n  - id: reliable-agents\n", encoding="utf-8",
    )

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="topics", research_topics_command="remove", topic_id="world-model",
    )) == 0
    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="profile", research_profile_command="remove-agenda",
        agenda_id="world-model-adjacent",
    )) == 0

    assert [item["id"] for item in yaml.safe_load((data / "topics.yaml").read_text())["topics"]] == ["agents"]
    assert [item["id"] for item in yaml.safe_load((data / "research-profile.yaml").read_text())["long_term_agenda"]] == ["reliable-agents"]
    backups = list((data / "backups").glob("preferences-*/*.yaml"))
    assert {path.name for path in backups} == {"topics.yaml", "research-profile.yaml"}
    assert "Removed research topic" in capsys.readouterr().out


def test_library_cli_validate_health_doctor_and_empty_recommendation(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (data / "topics.yaml").write_text("topics: []\n")
    (data / "research-profile.yaml").write_text("long_term_agenda: []\n")
    (data / "sources.yaml").write_text("schema_version: 1\nsources: []\n")

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="sources", research_sources_command="validate",
    )) == 0
    assert "Source Catalog valid" in capsys.readouterr().out

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="collect", source=None, dry_run=True,
    )) == 0
    assert "Research collection (dry-run)" in capsys.readouterr().out

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="health",
    )) == 0
    assert "Research Library health" in capsys.readouterr().out

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="doctor",
    )) == 0
    assert "Research Copilot doctor" in capsys.readouterr().out

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=True, threshold=0.72,
    )) == 0
    assert "No Research Item cleared" in capsys.readouterr().out


def test_evidence_cli_reviews_claim_and_writes_non_mutating_proposal(
    tmp_path, monkeypatch, capsys,
):
    from datetime import datetime, timezone

    from research_copilot.library import ResearchItemDraft, SourceEvidence
    from research_copilot.runtime import open_library

    home = tmp_path / "home"
    data = home / "research-copilot"
    data.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (data / "topics.yaml").write_text(yaml.safe_dump({
        "schema_version": 2,
        "topics": [{
            "id": "agent", "status": "active", "search_queries": ["agent"],
            "match_terms": ["agent"], "exclude_terms": [],
        }],
    }))
    profile = data / "research-profile.yaml"
    profile.write_text(yaml.safe_dump({
        "schema_version": 2,
        "long_term_agenda": [{
            "id": "agenda-a", "topic_ids": ["agent"],
            "current_beliefs": [{
                "id": "belief-a", "statement": "State improves reliability",
                "confidence": 0.8,
            }],
        }],
        "evidence_ledger": [],
    }, sort_keys=False))
    profile_before = profile.read_bytes()

    connection, repository = open_library(data / "library.db")
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    repository.upsert_source(
        source_id="test", provider="test", display_name="Test",
        source_type="paper", tier=1.0, now=now,
    )
    item = repository.upsert_item(
        ResearchItemDraft(title="Agent State", url="https://example.com/paper"),
        source=SourceEvidence("test"), discovered_at=now,
    )
    repository.record_feedback(item.item_id, kind="read", created_at=now)
    connection.close()

    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="evidence", research_evidence_command="add",
        item_id=item.item_id, belief="belief-a", prompt=None,
        relation="supports", claim_type="source_claim", strength=0.8,
        source_quality="primary", claim="Controlled evidence supports the claim.",
        rationale="Read in the results section",
    )) == 0
    assert "Pending evidence created" in capsys.readouterr().out

    connection, repository = open_library(data / "library.db")
    evidence_id = repository.list_evidence()[0]["id"]
    connection.close()
    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="evidence", research_evidence_command="review",
        evidence_id=evidence_id, accept=True, reject=False, note="verified",
    )) == 0
    assert cmd_research_copilot(argparse.Namespace(
        research_copilot_command="profile-proposal", dry_run=False,
    )) == 0

    proposals = list((data / "reports" / "profile-proposals").glob("*.yaml"))
    assert len(proposals) == 1
    proposal = yaml.safe_load(proposals[0].read_text())
    assert proposal["policy"]["mutates_profile"] is False
    assert proposal["belief_updates"][0]["belief_id"] == "belief-a"
    assert profile.read_bytes() == profile_before
