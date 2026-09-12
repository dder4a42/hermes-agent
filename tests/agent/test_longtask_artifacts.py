"""Artifact index + delivery manifest contracts (board item P6).

Behaviour only: the hash relationship between a registered path and its bytes,
the final-vs-scratch classification, and how the verifier's verdict relates to
the manifest. Nothing here reads source text and nothing freezes a snapshot of
current data.
"""

import hashlib
import json
from pathlib import Path

import pytest

from agent.longtask_board import (
    LongtaskBoardError,
    artifact_index_path,
    artifact_kind_for,
    delivery_manifest,
    load_artifact_index,
    register_artifacts,
    write_delivery_manifest,
)
from agent.longtask_verifier import verify_report


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _index_root(tmp_path: Path) -> Path:
    return tmp_path / "index"


# ---------------------------------------------------------------------------
# Where the index lives (profile-safe, not a hardcoded ~/.hermes)
# ---------------------------------------------------------------------------


def test_default_index_root_follows_the_profile_home(tmp_path, monkeypatch):
    """The index must follow HERMES_HOME, never a hardcoded ~/.hermes.

    Two profiles share a machine; an index pinned to the default home would let
    one profile read (and overwrite) another profile's delivery record.
    """
    home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_constants import get_hermes_home

    resolved_home = str(get_hermes_home())
    index = str(artifact_index_path("sess"))

    assert index.startswith(resolved_home)
    assert str(Path.home() / ".hermes") not in index


def test_index_and_manifest_sit_in_one_session_directory(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "a.txt").write_text("a", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s", [{"path": "a.txt", "kind": "final"}], workspace_root=ws, root=root
    )

    written = write_delivery_manifest("s", workspace_root=ws, root=root)

    assert artifact_index_path("s", root=root).exists()
    assert Path(written["manifest_path"]).parent == artifact_index_path("s", root=root).parent


# ---------------------------------------------------------------------------
# Registration: path + sha256 + producer
# ---------------------------------------------------------------------------


def test_register_records_hash_size_and_producer(tmp_path):
    ws = _workspace(tmp_path)
    payload = b"delivered\n"
    (ws / "out.txt").write_bytes(payload)
    root = _index_root(tmp_path)

    result = register_artifacts(
        "s",
        [{"path": "out.txt", "kind": "final"}],
        workspace_root=ws,
        producing_node_id="N1",
        child_session_id="child-1",
        delegation_id="deleg_1",
        root=root,
    )

    entry = result["registered"][0]
    assert entry["sha256"] == _sha(payload)
    assert entry["size"] == len(payload)
    assert entry["kind"] == "final"
    assert entry["rel_path"] == "out.txt"
    # The producing node / child session / delegation run are the join key back
    # to the board node and the report provenance.
    assert entry["producing_node_id"] == "N1"
    assert entry["child_session_id"] == "child-1"
    assert entry["delegation_id"] == "deleg_1"
    assert result["counts"] == {"final": 1, "scratch": 0, "total": 1}


def test_a_missing_file_is_registered_without_a_hash(tmp_path):
    """Registering a path that is not there records it, but hashes nothing."""
    ws = _workspace(tmp_path)
    root = _index_root(tmp_path)

    entry = register_artifacts(
        "s", [{"path": "never-written.txt", "kind": "final"}], workspace_root=ws, root=root
    )["registered"][0]

    assert entry["sha256"] is None
    assert entry["size"] is None
    assert delivery_manifest("s", workspace_root=ws, root=root)["missing"][0][
        "rel_path"
    ] == "never-written.txt"


def test_kind_must_be_stated_and_is_normalised(tmp_path):
    """final-vs-scratch is the whole point, so it is required and validated."""
    ws = _workspace(tmp_path)
    (ws / "a.txt").write_text("a", encoding="utf-8")
    root = _index_root(tmp_path)

    registered = register_artifacts(
        "s", [{"path": "a.txt", "kind": "temp"}], workspace_root=ws, root=root
    )
    assert registered["registered"][0]["kind"] == "scratch"

    with pytest.raises(LongtaskBoardError, match="Invalid artifact kind"):
        register_artifacts(
            "s", [{"path": "a.txt", "kind": "whatever"}], workspace_root=ws, root=root
        )
    with pytest.raises(LongtaskBoardError, match="Invalid artifact kind"):
        register_artifacts("s", [{"path": "a.txt"}], workspace_root=ws, root=root)


def test_re_registering_a_path_updates_it_in_place(tmp_path):
    """The path is the identity: re-registering must not duplicate the entry."""
    ws = _workspace(tmp_path)
    target = ws / "out.txt"
    target.write_text("v1", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s", [{"path": "out.txt", "kind": "scratch"}], workspace_root=ws, root=root
    )

    target.write_text("v2", encoding="utf-8")
    register_artifacts(
        "s", [{"path": "out.txt", "kind": "final"}], workspace_root=ws, root=root
    )

    entries = load_artifact_index("s", root=root)["artifacts"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "final"
    assert entries[0]["sha256"] == _sha(b"v2")


# ---------------------------------------------------------------------------
# The delivery manifest: final vs scratch, plus drift
# ---------------------------------------------------------------------------


def test_manifest_separates_deliverables_from_scratch(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "ship.txt").write_text("ok", encoding="utf-8")
    (ws / "tmp.txt").write_text("intermediate", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s",
        [
            {"path": "ship.txt", "kind": "final"},
            {"path": "tmp.txt", "kind": "scratch"},
        ],
        workspace_root=ws,
        root=root,
    )

    manifest = delivery_manifest("s", workspace_root=ws, root=root)

    assert [e["rel_path"] for e in manifest["final"]] == ["ship.txt"]
    assert [e["rel_path"] for e in manifest["scratch"]] == ["tmp.txt"]
    assert manifest["counts"] == {"final": 1, "scratch": 1, "missing": 0}


def test_manifest_flags_a_missing_deliverable_and_a_hash_drift(tmp_path):
    """A promised artifact that vanished, and one edited after registration."""
    ws = _workspace(tmp_path)
    (ws / "ship.txt").write_text("ok", encoding="utf-8")
    gone = ws / "gone.txt"
    gone.write_text("x", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s",
        [
            {"path": "ship.txt", "kind": "final"},
            {"path": "gone.txt", "kind": "final"},
        ],
        workspace_root=ws,
        root=root,
    )

    (ws / "ship.txt").write_text("edited after registration", encoding="utf-8")
    gone.unlink()

    manifest = delivery_manifest("s", workspace_root=ws, root=root)

    assert [e["rel_path"] for e in manifest["final"]] == ["ship.txt"]
    assert manifest["final"][0]["hash_matches"] is False
    assert [e["rel_path"] for e in manifest["missing"]] == ["gone.txt"]
    assert manifest["counts"] == {"final": 1, "scratch": 0, "missing": 1}


def test_manifest_is_persisted_as_readable_json(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "a.txt").write_text("a", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s", [{"path": "a.txt", "kind": "final"}], workspace_root=ws, root=root
    )

    written = write_delivery_manifest("s", workspace_root=ws, root=root)

    on_disk = json.loads(Path(written["manifest_path"]).read_text(encoding="utf-8"))
    assert on_disk == written["manifest"]


def test_kind_lookup_matches_the_registered_path(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "a.txt").write_text("a", encoding="utf-8")
    (ws / "b.txt").write_text("b", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s",
        [
            {"path": "a.txt", "kind": "final"},
            {"path": "b.txt", "kind": "scratch"},
        ],
        workspace_root=ws,
        root=root,
    )

    assert artifact_kind_for("s", "a.txt", workspace_root=ws, root=root) == "final"
    assert artifact_kind_for("s", "b.txt", workspace_root=ws, root=root) == "scratch"
    assert artifact_kind_for("s", "c.txt", workspace_root=ws, root=root) is None


# ---------------------------------------------------------------------------
# Verifier consumption: a scratch path cannot stand in for a deliverable
# ---------------------------------------------------------------------------


def _claim(ref: str, quote: str = "") -> dict:
    evidence = {"kind": "file", "ref": ref}
    if quote:
        evidence["quote"] = quote
    return {"claim": "The result was delivered", "evidence": [evidence]}


def test_verifier_refuses_a_scratch_path_as_a_deliverable(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "tmp-work.txt").write_text("intermediate\n", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s", [{"path": "tmp-work.txt", "kind": "scratch"}], workspace_root=ws, root=root
    )

    result = verify_report(
        {"claims": [_claim("tmp-work.txt", "intermediate")]},
        workspace_root=ws,
        artifact_index=load_artifact_index("s", root=root),
    )

    assert result["verdict"] == "rejected"
    check = result["claims"][0]["evidence"][0]
    assert check["artifact_kind"] == "scratch"
    assert check["ok"] is False
    assert "scratch" in check["reason"]


def test_verifier_accepts_and_labels_a_final_artifact(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "ship.txt").write_text("done\n", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s", [{"path": "ship.txt", "kind": "final"}], workspace_root=ws, root=root
    )

    result = verify_report(
        {"claims": [_claim("ship.txt", "done")]},
        workspace_root=ws,
        artifact_index=load_artifact_index("s", root=root),
    )

    assert result["verdict"] == "accepted"
    assert result["claims"][0]["evidence"][0]["artifact_kind"] == "final"


def test_without_an_index_the_file_checks_are_unchanged(tmp_path):
    """The classification is opt-in: an un-indexed workspace keeps old behavior."""
    ws = _workspace(tmp_path)
    (ws / "tmp-work.txt").write_text("intermediate\n", encoding="utf-8")

    result = verify_report(
        {"claims": [_claim("tmp-work.txt", "intermediate")]}, workspace_root=ws
    )

    assert result["verdict"] == "accepted"
    assert "artifact_kind" not in result["claims"][0]["evidence"][0]


def test_index_consumption_is_llm_free(tmp_path, monkeypatch):
    """The deterministic pass with a manifest attached calls no model."""
    ws = _workspace(tmp_path)
    (ws / "ship.txt").write_text("done\n", encoding="utf-8")
    root = _index_root(tmp_path)
    register_artifacts(
        "s", [{"path": "ship.txt", "kind": "final"}], workspace_root=ws, root=root
    )

    def _forbidden(**_kwargs):
        raise AssertionError("verify_report must not call a model")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", _forbidden)

    result = verify_report(
        {"claims": [_claim("ship.txt", "done")]},
        workspace_root=ws,
        artifact_index=load_artifact_index("s", root=root),
    )

    assert result["verdict"] == "accepted"
