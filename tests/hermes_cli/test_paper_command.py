import json

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class DummyCLI(CLICommandsMixin):
    pass


def test_cli_paper_command_prints_research_copilot_output(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    (data_dir / "topics.json").write_text(json.dumps({
        "topics": [
            {"id": "research-agent", "name": "Research Agent", "priority": 0.98, "status": "active"},
        ]
    }))

    DummyCLI()._handle_paper_command("paper topics")

    output = capsys.readouterr().out
    assert "Research Copilot Topics" in output
    assert "research-agent" in output
