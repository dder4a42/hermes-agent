import argparse
import json
from pathlib import Path


def test_weixin_bot_parser_dispatches_create():
    from hermes_cli.subcommands.weixin_bot import build_weixin_bot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")

    def handler(args):
        return args

    build_weixin_bot_parser(sub, cmd_weixin_bot=handler)
    ns = parser.parse_args([
        "weixin-bot",
        "create",
        "alice",
        "--source",
        "default",
        "--deliver",
        "weixin",
        "--clone",
    ])

    assert ns.command == "weixin-bot"
    assert ns.weixin_bot_command == "create"
    assert ns.profile_name == "alice"
    assert ns.source == "default"
    assert ns.deliver == "weixin"
    assert ns.clone is True
    assert ns.func is handler


def test_cmd_weixin_bot_create_initializes_profile_research_copilot(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    default_home = home / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    (default_home / "research-copilot").mkdir()
    (default_home / "research-copilot" / "config.json").write_text(json.dumps({"pipeline": "weixin"}))
    (default_home / "research-copilot" / "topics.json").write_text(json.dumps({"topics": [{"id": "agent"}]}))
    (default_home / "scripts").mkdir()
    (default_home / "scripts" / "paper-fetch.py").write_text("print('fetch')\n")
    (default_home / "scripts" / "paper-health.py").write_text("print('health')\n")

    from hermes_cli.weixin_bot_cmd import cmd_weixin_bot

    args = argparse.Namespace(
        weixin_bot_command="create",
        profile_name="alice",
        source="default",
        source_home=None,
        deliver="weixin",
        clone=False,
    )

    assert cmd_weixin_bot(args) == 0

    profile_home = default_home / "profiles" / "alice"
    assert json.loads((profile_home / "research-copilot" / "config.json").read_text())["pipeline"] == "weixin"
    assert (profile_home / "scripts" / "paper-fetch.py").exists()
    assert (profile_home / "cron" / "jobs.json").exists()
    output = capsys.readouterr().out
    assert "Weixin bot profile 'alice' is ready" in output
    assert "hermes -p alice gateway" in output
    assert "Weixin credentials" in output
