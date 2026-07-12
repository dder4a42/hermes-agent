import argparse
import json
import stat
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


def test_clone_config_alias_matches_plan_spec():
    """The plan calls the flag --clone-config; --clone is the canonical short form."""
    from hermes_cli.subcommands.weixin_bot import build_weixin_bot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_weixin_bot_parser(sub, cmd_weixin_bot=lambda args: args)
    ns = parser.parse_args(["weixin-bot", "create", "alice", "--clone-config"])
    assert ns.clone is True


def test_secret_flags_parsed():
    from hermes_cli.subcommands.weixin_bot import build_weixin_bot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_weixin_bot_parser(sub, cmd_weixin_bot=lambda args: args)
    ns = parser.parse_args([
        "weixin-bot", "create", "alice",
        "--weixin-token", "tok-1",
        "--weixin-account-id", "acct-1",
        "--allowed-user", "u1",
        "--allowed-user", "u2",
        "--dm-policy", "allowlist",
    ])
    assert ns.weixin_token == "tok-1"
    assert ns.weixin_account_id == "acct-1"
    assert ns.allowed_user == ["u1", "u2"]
    assert ns.dm_policy == "allowlist"


def test_list_subcommand_parses():
    from hermes_cli.subcommands.weixin_bot import build_weixin_bot_parser

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_weixin_bot_parser(sub, cmd_weixin_bot=lambda args: args)
    ns = parser.parse_args(["weixin-bot", "list", "--format", "json"])
    assert ns.weixin_bot_command == "list"
    assert ns.format == "json"


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
        weixin_token=None,
        weixin_account_id=None,
        allowed_user=None,
        dm_policy=None,
        force=False,
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


def test_cmd_weixin_bot_create_writes_env_when_flags_provided(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    default_home = home / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    (default_home / "research-copilot").mkdir()
    (default_home / "research-copilot" / "config.json").write_text("{}")
    (default_home / "research-copilot" / "topics.json").write_text('{"topics": []}')
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
        weixin_token="tok-secret-42",
        weixin_account_id="acct-99",
        allowed_user=["wxid_abc", "wxid_def"],
        dm_policy="allowlist",
        force=False,
    )

    assert cmd_weixin_bot(args) == 0

    profile_home = default_home / "profiles" / "alice"
    env_path = profile_home / ".env"
    assert env_path.exists()

    file_mode = stat.S_IMODE(env_path.stat().st_mode)
    assert file_mode == 0o600, f"env file mode is {oct(file_mode)}, expected 0o600"

    body = env_path.read_text()
    assert "WEIXIN_TOKEN=tok-secret-42" in body
    assert "WEIXIN_ACCOUNT_ID=acct-99" in body
    assert "WEIXIN_ALLOWED_USERS=wxid_abc,wxid_def" in body
    assert "WEIXIN_DM_POLICY=allowlist" in body

    output = capsys.readouterr().out
    # Secret values must NEVER appear in stdout.
    assert "tok-secret-42" not in output
    assert "acct-99" not in output
    assert "wxid_abc" not in output
    # Key names are fine — that's what tells the operator what was set.
    assert "WEIXIN_TOKEN" in output


def test_cmd_weixin_bot_create_refuses_to_overwrite_existing_env(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    default_home = home / ".hermes"
    default_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    (default_home / "research-copilot").mkdir()
    (default_home / "research-copilot" / "config.json").write_text("{}")
    (default_home / "research-copilot" / "topics.json").write_text('{"topics": []}')
    (default_home / "scripts").mkdir()
    (default_home / "scripts" / "paper-fetch.py").write_text("print('fetch')\n")
    (default_home / "scripts" / "paper-health.py").write_text("print('health')\n")

    profile_home = default_home / "profiles" / "alice"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("WEIXIN_TOKEN=old_value\n")

    from hermes_cli.weixin_bot_cmd import cmd_weixin_bot

    args = argparse.Namespace(
        weixin_bot_command="create",
        profile_name="alice",
        source="default",
        source_home=None,
        deliver="weixin",
        clone=False,
        weixin_token="new_value",
        weixin_account_id=None,
        allowed_user=None,
        dm_policy=None,
        force=False,
    )

    # Should exit with non-zero because we're not passing --force.
    assert cmd_weixin_bot(args) != 0

    # File must be unchanged.
    assert (profile_home / ".env").read_text() == "WEIXIN_TOKEN=old_value\n"
