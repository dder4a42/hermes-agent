import argparse
import json
from pathlib import Path

import yaml


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
    ns = parser.parse_args(["research", "collect", "--source", "rss", "--dry-run"])
    assert ns.command == "research"
    assert ns.research_copilot_command == "collect"
    assert ns.source == "rss"
    assert ns.dry_run is True


def test_research_parser_supports_preference_removal():
    from hermes_cli.subcommands.research_copilot import build_research_copilot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_research_copilot_parser(sub, cmd_research_copilot=lambda args: args)

    topics = parser.parse_args(["research", "topics", "remove", "world-model"])
    profile = parser.parse_args(["research", "profile", "remove-agenda", "world-model-adjacent"])
    assert topics.topic_id == "world-model"
    assert profile.agenda_id == "world-model-adjacent"


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
