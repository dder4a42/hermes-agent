"""Per-claim verification contracts (board item P3).

Behaviour only: every assertion is about how the verifier's output relates to
its input — which claim is contested, what was searched for, what the verdict
does when a claim is incidental or unreviewed. Nothing here reads source text.
"""

import json

import pytest

from agent.longtask_board import LongtaskBoardError, create_board, update_node
from agent.longtask_verifier import verify_report, verify_report_with_llm


def _fake_llm(script):
    """Scripted per-claim judge, in the fake style of test_longtask_verifier.

    ``script`` maps a claim's text to the review body it should return; claims
    absent from it come back non-contested. Returns (call_llm, calls) so a test
    can assert both the fan-out and what each call was asked about.
    """
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        request = json.loads(kwargs["messages"][1]["content"])
        body = script.get(
            request["claim"],
            '{"contested": false, "disconfirming_evidence": [], "required_repair": ""}',
        )

        class _Msg:
            content = body

        class _Choice:
            message = _Msg()

        class _Response:
            choices = [_Choice()]

        return _Response()

    return fake_call_llm, calls


def _contested(reason, repair):
    return json.dumps(
        {
            "contested": True,
            "disconfirming_evidence": [reason],
            "required_repair": repair,
        }
    )


def _reviewed_claims(calls):
    return [json.loads(call["messages"][1]["content"])["claim"] for call in calls]


def _file_claim(text, name, **extra):
    return {
        "claim": text,
        "evidence": [{"kind": "file", "ref": name}],
        **extra,
    }


def test_a_contested_claim_does_not_taint_a_verified_sibling(monkeypatch, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    fake, calls = _fake_llm(
        {"A is done": _contested("no run output shown", "attach the command output")}
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {"claims": [_file_claim("A is done", "a.txt"), _file_claim("B is done", "b.txt")]},
        workspace_root=tmp_path,
        llm_config={"enabled": True},
    )

    claim_a, claim_b = result["claims"]
    assert claim_a["contested"] is True
    assert claim_a["status"] == "contested"
    assert claim_a["disconfirming_evidence"] == ["no run output shown"]
    assert claim_a["required_repair"] == "attach the command output"
    assert claim_a["load_bearing"] is True

    # The sibling is judged on its own evidence and is not pulled down with it.
    assert claim_b["contested"] is False
    assert claim_b["status"] == "accepted"
    assert [c["claim"] for c in result["accepted_claims"]] == ["B is done"]
    assert result["verdict"] == "needs_followup"
    # One review per claim, not one review of the whole report.
    assert len(calls) == 2
    assert sorted(_reviewed_claims(calls)) == ["A is done", "B is done"]


def test_fabricated_quote_fails_the_claim_and_names_what_was_searched(tmp_path):
    (tmp_path / "note.txt").write_text(
        "line one\nline two: the real content\nline three\n", encoding="utf-8"
    )

    result = verify_report(
        {
            "claims": [
                {
                    "claim": "The note says the real content",
                    "evidence": [
                        {"kind": "file", "ref": "note.txt:2", "quote": "the real content"}
                    ],
                },
                {
                    "claim": "The note says something it does not",
                    "evidence": [
                        {
                            "kind": "file",
                            "ref": "note.txt:2",
                            "quote": "a fabricated sentence about unicorns",
                        }
                    ],
                },
            ]
        },
        workspace_root=tmp_path,
    )

    present, fabricated = result["claims"]
    assert present["deterministic_ok"] is True
    assert present["disconfirming_evidence"] == []
    assert present["status"] == "accepted"

    assert fabricated["deterministic_ok"] is False
    assert fabricated["status"] == "failed"
    assert fabricated["disconfirming_evidence"] == [
        {
            "kind": "file",
            "ref": "note.txt:2",
            "reason": "cited quote not found in note.txt near line 2",
            "searched_for": "a fabricated sentence about unicorns",
            "line": 2,
        }
    ]
    assert fabricated["required_repair"]
    assert [c["claim"] for c in result["accepted_claims"]] == [
        "The note says the real content"
    ]
    assert result["verdict"] == "needs_followup"


def test_quote_check_normalizes_whitespace_and_honors_truncation(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def alpha():\n    beta = 1\n    gamma = 2\n", encoding="utf-8"
    )

    result = verify_report(
        {
            "claims": [
                {
                    "claim": "The function computes beta and gamma",
                    "evidence": [
                        {
                            "kind": "file",
                            "ref": "sample.py:2",
                            "quote": "beta = 1    gamma = 2",
                        }
                    ],
                },
                {
                    "claim": "The function is named alpha",
                    "evidence": [
                        {"kind": "file", "ref": "sample.py:1", "quote": "def alpha(): ..."}
                    ],
                },
            ]
        },
        workspace_root=tmp_path,
    )

    for record in result["claims"]:
        assert record["deterministic_ok"] is True, record["disconfirming_evidence"]
    assert result["verdict"] == "accepted"


def test_deterministic_failure_skips_the_judge_and_still_names_the_quote(
    monkeypatch, tmp_path
):
    (tmp_path / "note.txt").write_text("real content\n", encoding="utf-8")
    fake, calls = _fake_llm({})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {
            "claims": [
                {
                    "claim": "Fabricated",
                    "evidence": [
                        {"kind": "file", "ref": "note.txt", "quote": "not in the file"}
                    ],
                }
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True},
    )

    # Already decided: spending the review budget on it could push a claim that
    # can still change the verdict out of the cap.
    assert calls == []
    record = result["claims"][0]
    assert record["status"] == "failed"
    assert record["disconfirming_evidence"][0]["searched_for"] == "not in the file"
    assert result["verdict"] == "rejected"


def test_contested_non_load_bearing_claim_does_not_block_acceptance(
    monkeypatch, tmp_path
):
    (tmp_path / "main.txt").write_text("main\n", encoding="utf-8")
    (tmp_path / "aside.txt").write_text("aside\n", encoding="utf-8")
    fake, _ = _fake_llm(
        {
            "Incidental observation": _contested(
                "not checkable from the evidence", "drop it or support it"
            )
        }
    )
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {
            "claims": [
                _file_claim("The shipped file exists", "main.txt"),
                _file_claim(
                    "Incidental observation", "aside.txt", load_bearing=False
                ),
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True},
    )

    # An explicitly incidental claim being disputed cannot hold the item open...
    assert result["verdict"] == "accepted"
    assert [c["claim"] for c in result["accepted_claims"]] == ["The shipped file exists"]
    # ...but it is still reported, with its repair, instead of vanishing.
    assert [c["claim"] for c in result["contested_claims"]] == ["Incidental observation"]
    assert result["contested_claims"][0]["load_bearing"] is False
    assert result["contested_claims"][0]["required_repair"] == "drop it or support it"


def test_incidental_claim_cannot_excuse_a_deterministic_failure(monkeypatch, tmp_path):
    (tmp_path / "good.txt").write_text("present\n", encoding="utf-8")
    fake, calls = _fake_llm({})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {
            "claims": [
                {"claim": "Substantive", "evidence": [{"kind": "file", "ref": "good.txt"}]},
                {
                    "claim": "Incidental aside",
                    "load_bearing": False,
                    "evidence": [
                        {
                            "kind": "file",
                            "ref": "good.txt",
                            "quote": "never written in this file",
                        }
                    ],
                },
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True},
    )

    # An absent quote is a fact, not an opinion: `load_bearing: false` can
    # excuse a contest, but it cannot excuse a fabricated citation.
    assert _reviewed_claims(calls) == ["Substantive"]
    assert result["claims"][1]["load_bearing"] is False
    assert result["claims"][1]["status"] == "failed"
    assert result["verdict"] == "needs_followup"


def test_a_report_without_claims_is_never_accepted_with_the_judge_on(
    monkeypatch, tmp_path
):
    fake, calls = _fake_llm({})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {"claims": []}, workspace_root=tmp_path, llm_config={"enabled": True}
    )

    # "No claim was contested" must not be read as "nothing to contest,
    # therefore accepted".
    assert calls == []
    assert result["llm_verification"]["enabled"] is True
    assert result["verdict"] == "needs_followup"


def test_cap_leaves_the_rest_unverified_and_is_never_accepted(monkeypatch, tmp_path):
    names = ("one.txt", "two.txt", "three.txt")
    for name in names:
        (tmp_path / name).write_text(name, encoding="utf-8")
    fake, calls = _fake_llm({})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {
            "claims": [
                _file_claim(f"Claim {index}", name)
                for index, name in enumerate(names, start=1)
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True, "max_claim_reviews": 1},
    )

    assert len(calls) == 1
    assert [r["claim"] for r in result["claims"] if r["status"] == "unverified"] == [
        "Claim 2",
        "Claim 3",
    ]
    assert [r["claim"] for r in result["unverified_claims"]] == ["Claim 2", "Claim 3"]
    assert all(r["reviewed"] is False for r in result["claims"][1:])
    # Unverified is never counted as accepted, so an unreviewed load-bearing
    # claim keeps the item open even though nothing failed.
    assert result["verdict"] == "needs_followup"
    assert [c["claim"] for c in result["accepted_claims"]] == ["Claim 1"]


def test_cap_spends_its_budget_on_load_bearing_claims_first(monkeypatch, tmp_path):
    (tmp_path / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
    fake, calls = _fake_llm({})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    result = verify_report_with_llm(
        {
            "claims": [
                _file_claim("Incidental", "one.txt", load_bearing=False),
                _file_claim("Critical", "two.txt"),
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True, "max_claim_reviews": 1},
    )

    assert _reviewed_claims(calls) == ["Critical"]
    assert [r["claim"] for r in result["unverified_claims"]] == ["Incidental"]
    # The claim the cap had to skip is incidental, so the load-bearing claim
    # still decides acceptance.
    assert result["verdict"] == "accepted"


def test_old_shape_verification_dict_still_gates_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"longtask": {"require_verification_before_unlock": True}},
    )
    create_board(tmp_path, "s", "obj", [{"node_id": "N1", "goal": "g"}])

    # Boards written before per-claim verification carry only the aggregate
    # keys. The gate reads `verdict`, so those stored dicts keep working.
    update_node(
        tmp_path,
        "s",
        "N1",
        verification={
            "verdict": "needs_followup",
            "accepted_claims": [],
            "rejected_claims": [],
            "missing_evidence": ["too vague"],
            "summary_for_parent": "Needs more evidence.",
        },
    )
    with pytest.raises(LongtaskBoardError, match="needs_followup"):
        update_node(tmp_path, "s", "N1", resolution="resolved")

    update_node(tmp_path, "s", "N1", verification={"verdict": "accepted"})
    assert (
        update_node(tmp_path, "s", "N1", resolution="resolved")["node"]["resolution"]
        == "resolved"
    )


def test_per_claim_verification_persists_and_unlocks_through_the_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"longtask": {"require_verification_before_unlock": True}},
    )
    (tmp_path / "out.txt").write_text("ok\n", encoding="utf-8")
    fake, _ = _fake_llm({})
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake)

    verification = verify_report_with_llm(
        {
            "claims": [
                {
                    "claim": "Out exists",
                    "evidence": [
                        {"kind": "file", "ref": "out.txt:1", "quote": "ok"}
                    ],
                }
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True},
    )
    create_board(tmp_path, "s", "obj", [{"node_id": "N1", "goal": "g"}])

    stored = update_node(tmp_path, "s", "N1", verification=verification)["node"][
        "verification"
    ]

    # The per-claim review survives the board round-trip...
    assert stored["claims"][0]["contested"] is False
    assert stored["claims"][0]["required_repair"] == ""
    assert stored["claims"][0]["disconfirming_evidence"] == []
    assert stored["verdict"] == "accepted"
    # ...and the gate still reads the same aggregate key it always read.
    assert (
        update_node(tmp_path, "s", "N1", resolution="resolved")["node"]["resolution"]
        == "resolved"
    )
