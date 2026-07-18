from __future__ import annotations

import argparse

import pytest


@pytest.mark.parametrize(
    ("module_name", "command"),
    [
        ("research_copilot.scripts.library_collect", "collect"),
        ("research_copilot.scripts.library_health", "health"),
        ("research_copilot.scripts.library_recommend", "recommend"),
        ("research_copilot.scripts.library_scout", "scout"),
    ],
)
def test_cron_shims_only_dispatch_repository_cli(monkeypatch, module_name, command):
    calls = []
    monkeypatch.setattr(
        "hermes_cli.research_copilot_cmd.cmd_research_copilot",
        lambda args: calls.append(args) or 0,
    )
    module = __import__(module_name, fromlist=["main"])
    assert module.main() == 0
    assert len(calls) == 1
    assert isinstance(calls[0], argparse.Namespace)
    assert calls[0].research_copilot_command == command


def test_bundled_cron_module_runs_through_real_scheduler_subprocess(monkeypatch, tmp_path):
    """Exercise the cwd/interpreter boundary that in-process imports miss."""
    from cron.scheduler import _run_job_script

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    success, output = _run_job_script(
        "module:research_copilot.scripts.library_health"
    )

    assert success is True, output
    assert "Research Library" in output


def test_bundled_cron_module_rejects_unregistered_import():
    from cron.scheduler import _run_job_script

    success, output = _run_job_script("module:os")

    assert success is False
    assert "unregistered bundled cron module" in output
