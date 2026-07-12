import asyncio
import json

from gateway.slash_commands import GatewaySlashCommandsMixin


class DummyGateway(GatewaySlashCommandsMixin):
    pass


class DummyEvent:
    def __init__(self, args: str):
        self._args = args

    def get_command_args(self) -> str:
        return self._args


def test_gateway_paper_command_uses_profile_scoped_research_copilot_state(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    (data_dir / "topics.json").write_text(json.dumps({
        "topics": [
            {"id": "research-agent", "name": "Research Agent", "priority": 0.98, "status": "active"},
        ]
    }))

    output = asyncio.run(DummyGateway()._handle_paper_command(DummyEvent("topics")))

    assert "Research Copilot Topics" in output
    assert "research-agent" in output
