from __future__ import annotations

import subprocess


def _git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    )


def _config(vault, **overrides):
    sync = {
        "enabled": True,
        "remote": "origin",
        "branch": "main",
        "max_changed_files": 10,
        "max_deleted_files": 0,
        "max_untracked_files": 5,
        "forbidden_prefixes": [".hermes-archive/"],
    }
    sync.update(overrides)
    return {
        "research_copilot": {
            "wiki": {
                "vault_path": str(vault),
                "library_subdir": "Research Library",
                "git_sync": sync,
            },
        },
    }


def _repository(tmp_path):
    vault = tmp_path / "vault"
    remote = tmp_path / "remote.git"
    vault.mkdir()
    (vault / "Research Library").mkdir()
    (vault / "note.md").write_text("initial\n")
    _git(vault, "init")
    _git(vault, "checkout", "-b", "main")
    _git(vault, "config", "user.name", "Test")
    _git(vault, "config", "user.email", "test@example.invalid")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(vault, "remote", "add", "origin", str(remote))
    _git(vault, "push", "-u", "origin", "main")
    return vault, remote


def test_publish_check_blocks_forbidden_and_large_change(monkeypatch, tmp_path):
    from hermes_cli import config as config_module
    from research_copilot.vault_sync import check_vault_publication

    vault, _remote = _repository(tmp_path)
    (vault / ".hermes-archive").mkdir()
    (vault / ".hermes-archive" / "run.json").write_text("{}")
    (vault / "second.md").write_text("dirty")
    monkeypatch.setattr(
        config_module, "load_config_readonly",
        lambda: _config(vault, max_changed_files=1),
    )

    result = check_vault_publication()
    assert result.allowed is False
    assert any("forbidden paths" in reason for reason in result.reasons)
    assert any("exceed limit" in reason for reason in result.reasons)


def test_sync_vault_exercises_real_local_git_remote(monkeypatch, tmp_path):
    from hermes_cli import config as config_module
    from research_copilot.vault_sync import sync_vault

    vault, remote = _repository(tmp_path)
    (vault / "note.md").write_text("updated\n")
    monkeypatch.setattr(config_module, "load_config_readonly", lambda: _config(vault))

    success, output = sync_vault()
    assert success is True
    assert output.startswith("vault synced: ")
    local = _git(vault, "rev-parse", "HEAD").stdout.strip()
    remote_head = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert remote_head == local
