import argparse
import json
from pathlib import Path


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
    assert (profile_home / "scripts" / "paper-fetch.py").exists()
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
