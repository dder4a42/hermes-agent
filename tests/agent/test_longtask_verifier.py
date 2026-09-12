from agent.longtask_verifier import verify_report, verify_report_with_llm


def test_verify_report_accepts_file_evidence(tmp_path):
    evidence_file = tmp_path / "out.txt"
    evidence_file.write_text("ok", encoding="utf-8")

    result = verify_report(
        {
            "claims": [
                {
                    "claim": "Output exists",
                    "evidence": [{"kind": "file", "ref": "out.txt"}],
                }
            ]
        },
        workspace_root=tmp_path,
    )

    assert result["verdict"] == "accepted"
    assert result["accepted_claims"][0]["claim"] == "Output exists"


def test_verify_report_rejects_missing_evidence():
    result = verify_report({"claims": [{"claim": "Unsupported", "evidence": []}]})

    assert result["verdict"] == "rejected"
    assert result["missing_evidence"]


def test_verify_report_flags_partial_claims(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("ok", encoding="utf-8")

    result = verify_report(
        {
            "claims": [
                {"claim": "Good", "evidence": [{"kind": "file", "ref": "good.txt"}]},
                {"claim": "Bad", "evidence": [{"kind": "file", "ref": "missing.txt"}]},
            ]
        },
        workspace_root=tmp_path,
    )

    assert result["verdict"] == "needs_followup"
    assert len(result["accepted_claims"]) == 1
    assert len(result["rejected_claims"]) == 1


def test_verify_report_with_llm_overrides_verdict(monkeypatch, tmp_path):
    evidence_file = tmp_path / "out.txt"
    evidence_file.write_text("ok", encoding="utf-8")
    calls = []

    class _Msg:
        content = (
            '{"verdict":"needs_followup","accepted_claims":[],'
            '"rejected_claims":[],"missing_evidence":["too vague"],'
            '"summary_for_parent":"Needs more evidence."}'
        )

    class _Choice:
        message = _Msg()

    class _Response:
        choices = [_Choice()]

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    result = verify_report_with_llm(
        {
            "claims": [
                {
                    "claim": "Output exists",
                    "evidence": [{"kind": "file", "ref": "out.txt"}],
                }
            ]
        },
        node_goal="verify output",
        objective="ship",
        workspace_root=tmp_path,
        llm_config={"enabled": True, "model": "judge", "timeout": 5},
    )

    assert calls
    assert calls[0]["task"] == "longtask_verifier"
    assert calls[0]["model"] == "judge"
    assert result["verdict"] == "needs_followup"
    assert result["llm_verification"]["summary_for_parent"] == "Needs more evidence."


def test_verify_report_with_llm_failure_keeps_deterministic(monkeypatch, tmp_path):
    evidence_file = tmp_path / "out.txt"
    evidence_file.write_text("ok", encoding="utf-8")

    def fake_call_llm(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    result = verify_report_with_llm(
        {
            "claims": [
                {
                    "claim": "Output exists",
                    "evidence": [{"kind": "file", "ref": "out.txt"}],
                }
            ]
        },
        workspace_root=tmp_path,
        llm_config={"enabled": True},
    )

    assert result["verdict"] == "accepted"
    assert result["llm_verification"]["error"] == "provider down"
