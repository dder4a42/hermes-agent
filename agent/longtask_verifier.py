"""Deterministic verifier for longtask claim-evidence reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import re


def verify_report(
    report: Dict[str, Any],
    *,
    workspace_root: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Verify a subagent report without running another model."""
    if not isinstance(report, dict):
        return _result(
            "rejected",
            rejected_claims=[],
            missing_evidence=["report is not an object"],
        )

    claims = report.get("claims")
    if not isinstance(claims, list) or not claims:
        return _result(
            "needs_followup",
            rejected_claims=[],
            missing_evidence=["report has no claims"],
        )

    accepted = []
    rejected = []
    missing = []
    root = Path(workspace_root).expanduser().resolve() if workspace_root else None

    for index, claim in enumerate(claims, start=1):
        if not isinstance(claim, dict):
            rejected.append({"index": index, "reason": "claim is not an object"})
            continue
        text = str(claim.get("claim") or "").strip()
        if not text:
            rejected.append({"index": index, "reason": "claim text is empty"})
            continue
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            missing.append(f"claim {index} has no evidence")
            rejected.append({"claim": text, "reason": "missing evidence"})
            continue
        checks = [_check_evidence(item, root=root) for item in evidence]
        bad = [item for item in checks if not item["ok"]]
        if bad:
            rejected.append({
                "claim": text,
                "reason": "evidence failed checks",
                "evidence": bad,
            })
            for item in bad:
                missing.append(f"claim {index}: {item['reason']}")
            continue
        accepted.append({"claim": text, "evidence": checks})

    if accepted and not rejected and not missing:
        verdict = "accepted"
    elif accepted:
        verdict = "needs_followup"
    else:
        verdict = "rejected"

    return _result(
        verdict,
        accepted_claims=accepted,
        rejected_claims=rejected,
        missing_evidence=missing,
    )


def verify_report_with_llm(
    report: Dict[str, Any],
    *,
    node_goal: str = "",
    objective: str = "",
    workspace_root: Optional[str | Path] = None,
    llm_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verify a report with deterministic checks plus an optional LLM judge."""
    deterministic = verify_report(report, workspace_root=workspace_root)
    cfg = llm_config or {}
    if not cfg.get("enabled"):
        return deterministic

    try:
        llm = _call_llm_verifier(
            report=report,
            deterministic=deterministic,
            node_goal=node_goal,
            objective=objective,
            cfg=cfg,
        )
    except Exception as exc:
        merged = dict(deterministic)
        merged["llm_verification"] = {
            "enabled": True,
            "error": str(exc),
        }
        merged["summary_for_parent"] = (
            deterministic["summary_for_parent"]
            + " LLM verifier failed; deterministic verdict retained."
        )
        return merged

    merged = dict(deterministic)
    merged["llm_verification"] = llm
    if llm.get("verdict") in {"accepted", "rejected", "needs_followup"}:
        merged["verdict"] = llm["verdict"]
    if llm.get("summary_for_parent"):
        merged["summary_for_parent"] = str(llm["summary_for_parent"])
    return merged


def _call_llm_verifier(
    *,
    report: Dict[str, Any],
    deterministic: Dict[str, Any],
    node_goal: str,
    objective: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    prompt = {
        "objective": objective,
        "node_goal": node_goal,
        "report": report,
        "deterministic_verification": deterministic,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict verifier for long-horizon agent work. "
                "Judge only whether the report's claims are supported by the "
                "provided evidence. Do not redo the task and do not accept "
                "claims without evidence. Return only JSON with keys: verdict "
                "(accepted|rejected|needs_followup), accepted_claims, "
                "rejected_claims, missing_evidence, summary_for_parent."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt, ensure_ascii=False, sort_keys=True),
        },
    ]
    response = call_llm(
        task="longtask_verifier",
        provider=cfg.get("provider") or None,
        model=cfg.get("model") or None,
        base_url=cfg.get("base_url") or None,
        api_key=cfg.get("api_key") or None,
        api_mode=cfg.get("api_mode") or None,
        messages=messages,
        temperature=0,
        max_tokens=int(cfg.get("max_tokens") or 1200),
        timeout=float(cfg.get("timeout") or 30),
    )
    text = (extract_content_or_reasoning(response) or "").strip()
    data = _parse_json_object(text)
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in {"accepted", "rejected", "needs_followup"}:
        raise ValueError(f"LLM verifier returned invalid verdict: {verdict!r}")
    return {
        "enabled": True,
        "verdict": verdict,
        "accepted_claims": data.get("accepted_claims") if isinstance(data.get("accepted_claims"), list) else [],
        "rejected_claims": data.get("rejected_claims") if isinstance(data.get("rejected_claims"), list) else [],
        "missing_evidence": data.get("missing_evidence") if isinstance(data.get("missing_evidence"), list) else [],
        "summary_for_parent": str(data.get("summary_for_parent") or "").strip(),
    }


def _parse_json_object(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM verifier response must be a JSON object")
    return data


def _check_evidence(item: Any, *, root: Optional[Path]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"ok": False, "reason": "evidence is not an object"}
    kind = str(item.get("kind") or item.get("type") or "").strip().lower()
    ref = str(item.get("ref") or "").strip()
    quote = str(item.get("quote") or item.get("output") or "").strip()

    if kind == "file":
        if not ref:
            return {"ok": False, "kind": kind, "reason": "file evidence missing ref"}
        path = Path(ref).expanduser()
        if not path.is_absolute() and root is not None:
            path = root / path
        ok = path.exists()
        return {
            "ok": ok,
            "kind": kind,
            "ref": ref,
            "reason": "file exists" if ok else "file does not exist",
        }
    if kind == "command":
        has_command = bool(ref or item.get("command"))
        has_result = bool(quote or item.get("exit_code") is not None)
        return {
            "ok": has_command and has_result,
            "kind": kind,
            "ref": ref or str(item.get("command") or ""),
            "reason": "command evidence has command and result"
            if has_command and has_result
            else "command evidence needs command/ref and quote/output or exit_code",
        }
    if kind == "url":
        ok = ref.startswith(("http://", "https://"))
        return {
            "ok": ok,
            "kind": kind,
            "ref": ref,
            "reason": "url evidence has URL" if ok else "url evidence ref must be http(s)",
        }
    if kind == "observation":
        ok = bool(ref or quote)
        return {
            "ok": ok,
            "kind": kind,
            "ref": ref,
            "reason": "observation has content" if ok else "observation needs ref or quote",
        }
    return {"ok": False, "kind": kind, "ref": ref, "reason": "unknown evidence kind"}


def _result(
    verdict: str,
    *,
    accepted_claims: Optional[List[Dict[str, Any]]] = None,
    rejected_claims: Optional[List[Dict[str, Any]]] = None,
    missing_evidence: Optional[List[str]] = None,
) -> Dict[str, Any]:
    accepted = accepted_claims or []
    rejected = rejected_claims or []
    missing = missing_evidence or []
    return {
        "verdict": verdict,
        "accepted_claims": accepted,
        "rejected_claims": rejected,
        "missing_evidence": missing,
        "summary_for_parent": (
            f"Verification {verdict}: {len(accepted)} accepted claim(s), "
            f"{len(rejected)} rejected claim(s), "
            f"{len(missing)} missing evidence item(s)."
        ),
    }
