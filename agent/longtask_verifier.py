"""Verifier for longtask claim-evidence reports.

Two layers, deliberately asymmetric:

* Deterministic checks run first and cost nothing. They answer the objective
  questions — does the cited file exist, does the cited quote actually occur in
  it — and their failures are hard facts, not opinions.
* A per-CLAIM LLM review runs second and only when configured. One review per
  claim, never one review of the whole report: a single report-level judgement
  lets one vague claim taint a grounded sibling, and it cannot say *which*
  claim needs repair. Each review yields ``contested`` /
  ``disconfirming_evidence`` / ``required_repair`` for that claim alone.

Resolution is asymmetric as well: a claim is load-bearing unless it explicitly
says ``load_bearing: false``, and only load-bearing claims can hold an item
open. An explicitly incidental claim the reviewer disputes is still reported,
but it does not block acceptance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import json
import re


# Cap on how many claims one report may send to the LLM. A 20-claim report must
# not turn into 20 unbounded model calls: the cap bounds the spend, and claims
# left over are reported ``unverified`` — which is NEVER counted as accepted —
# so the parent sees the gap instead of inheriting a silent pass.
DEFAULT_MAX_CLAIM_REVIEWS = 8

# Line window used when a ref carries a line anchor. Wide enough to tolerate the
# line drift a normal edit causes, narrow enough that a quote lifted from
# somewhere else in the file does not pass as "near the cited line".
_QUOTE_WINDOW_LINES = 5

_LINE_ANCHOR_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<col>\d+))?$")

_TRUNCATION_MARKERS = ("...", "\u2026")


# The rules the deterministic pass enforces, phrased for the judge. A claim is
# only sent the constraints that bear on its own evidence kinds, so the review
# judges against the same bar the host enforces — not a different one.
_CONSTRAINT_LIBRARY = {
    "evidence": "The claim must carry at least one piece of evidence.",
    "file": "A `file` ref must name a file that exists in the workspace.",
    "quote": (
        "If `file`/test evidence cites a quote, that quote must actually occur "
        "in the cited file near the cited line (whitespace-normalised; a "
        "trailing `...` marks an intentional truncation)."
    ),
    "command": (
        "`command` evidence must carry both the command and its result "
        "(a quote/output or an exit_code)."
    ),
    "url": "`url` evidence ref must be an http(s) URL.",
    "observation": "`observation` evidence must carry a ref or a quote.",
    "scope": (
        "The claim must stay inside the node goal and the board objective, and "
        "must not assert more than its evidence shows."
    ),
}


def verify_report(
    report: Dict[str, Any],
    *,
    workspace_root: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Verify a subagent report without running another model.

    Per-claim records are returned under ``claims`` next to the aggregate keys,
    so an LLM review can be layered on per claim without re-deriving the
    deterministic part (and so a deterministic failure is visible per claim,
    with the quote that was searched for).
    """
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
    records: List[Dict[str, Any]] = []
    root = Path(workspace_root).expanduser().resolve() if workspace_root else None

    for index, claim in enumerate(claims, start=1):
        if not isinstance(claim, dict):
            rejected.append({"index": index, "reason": "claim is not an object"})
            records.append(_claim_record(
                index,
                "",
                load_bearing=True,
                checks=[],
                bad=[{"ok": False, "reason": "claim is not an object"}],
            ))
            continue
        text = str(claim.get("claim") or "").strip()
        load_bearing = _is_load_bearing(claim)
        if not text:
            rejected.append({"index": index, "reason": "claim text is empty"})
            records.append(_claim_record(
                index,
                "",
                load_bearing=load_bearing,
                checks=[],
                bad=[{"ok": False, "reason": "claim text is empty"}],
            ))
            continue
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            missing.append(f"claim {index} has no evidence")
            rejected.append({"claim": text, "reason": "missing evidence"})
            records.append(_claim_record(
                index,
                text,
                load_bearing=load_bearing,
                checks=[],
                bad=[{"ok": False, "reason": "claim has no evidence"}],
            ))
            continue
        checks = [_check_evidence(item, root=root) for item in evidence]
        bad = [item for item in checks if not item["ok"]]
        records.append(_claim_record(
            index,
            text,
            load_bearing=load_bearing,
            checks=checks,
            bad=bad,
        ))
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

    result = _result(
        verdict,
        accepted_claims=accepted,
        rejected_claims=rejected,
        missing_evidence=missing,
    )
    result["claims"] = records
    return result


def verify_report_with_llm(
    report: Dict[str, Any],
    *,
    node_goal: str = "",
    objective: str = "",
    workspace_root: Optional[str | Path] = None,
    llm_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verify a report with deterministic checks plus per-claim LLM reviews.

    One review per claim instead of one per report, capped by
    ``max_claim_reviews`` (``longtask.verifier_max_claim_reviews``). With no LLM
    configured this returns exactly the deterministic result — the degradation
    path is unchanged.
    """
    deterministic = verify_report(report, workspace_root=workspace_root)
    cfg = llm_config or {}
    if not cfg.get("enabled"):
        return deterministic

    records = [dict(record) for record in (deterministic.get("claims") or [])]
    if not records:
        # Nothing to adjudicate per claim (a report with no claims at all). The
        # deterministic verdict stands: "no claim contested" must NOT be read as
        # "nothing to contest, therefore accepted".
        merged = dict(deterministic)
        merged["llm_verification"] = {
            "enabled": True,
            "reviewed_claims": 0,
            "max_claim_reviews": _max_claim_reviews(cfg),
            "errors": [],
        }
        return merged

    reviews, errors = _review_claims_with_llm(
        report=report,
        records=records,
        node_goal=node_goal,
        objective=objective,
        cfg=cfg,
    )

    if not reviews and errors:
        # Every review call failed. Same contract as when the judge was a single
        # whole-report call: the deterministic verdict stands and the failure is
        # recorded, so nobody reads "no model ever looked at this" as a pass.
        merged = dict(deterministic)
        merged["llm_verification"] = {
            "enabled": True,
            "error": errors[0],
            "errors": errors,
        }
        merged["summary_for_parent"] = (
            deterministic["summary_for_parent"]
            + " LLM verifier failed; deterministic verdict retained."
        )
        return merged

    return _merge_llm_reviews(
        deterministic,
        records,
        reviews,
        errors,
        _max_claim_reviews(cfg),
    )


def _review_claims_with_llm(
    *,
    report: Dict[str, Any],
    records: List[Dict[str, Any]],
    node_goal: str,
    objective: str,
    cfg: Dict[str, Any],
) -> Tuple[Dict[int, Dict[str, Any]], List[str]]:
    """Fan out one LLM review per claim, bounded by the configured cap.

    Deterministically failed claims are not sent to the judge: they are already
    decided, and spending the cap on them would push a claim that could still
    change the verdict out of the budget.
    """
    report_claims = report.get("claims") if isinstance(report, dict) else None
    if not isinstance(report_claims, list):
        report_claims = []
    cap = _max_claim_reviews(cfg)

    candidates = [r for r in records if r["deterministic_ok"] and r["claim"]]
    # Most load-bearing first: the cap must never be spent on a claim that could
    # not block resolution while a claim that can goes unreviewed. Within that,
    # claims resting on more evidence get the judge's attention first.
    ordered = sorted(
        candidates,
        key=lambda r: (not r["load_bearing"], -len(r.get("evidence") or []), r["index"]),
    )
    selected = ordered[:cap]

    reviews: Dict[int, Dict[str, Any]] = {}
    errors: List[str] = []
    for record in selected:
        raw = (
            report_claims[record["index"] - 1]
            if record["index"] - 1 < len(report_claims)
            else {}
        )
        evidence = raw.get("evidence") if isinstance(raw, dict) else []
        if not isinstance(evidence, list):
            evidence = []
        try:
            reviews[record["index"]] = _call_llm_claim_review(
                claim_text=record["claim"],
                evidence=evidence,
                constraints=_applicable_constraints(
                    evidence, node_goal=node_goal, objective=objective
                ),
                deterministic_evidence=record.get("evidence") or [],
                node_goal=node_goal,
                objective=objective,
                cfg=cfg,
            )
        except Exception as exc:
            # One claim's review failing must not discard the others; the
            # failure is recorded on that claim and it stays unverified.
            errors.append(str(exc))
            record["review_status"] = "error"
            record["review_error"] = str(exc)
    for record in ordered[cap:]:
        record["review_status"] = "unverified_cap"
    for record in records:
        if not record["deterministic_ok"]:
            record["review_status"] = "skipped_deterministic_failure"
        elif not record["claim"]:
            record["review_status"] = "unreviewed"
    return reviews, errors


def _merge_llm_reviews(
    deterministic: Dict[str, Any],
    records: List[Dict[str, Any]],
    reviews: Dict[int, Dict[str, Any]],
    errors: List[str],
    cap: int,
) -> Dict[str, Any]:
    merged = dict(deterministic)

    for record in records:
        review = reviews.get(record["index"])
        if review:
            record.update(review)
        record["status"] = _record_status(record, llm_enabled=True)

    contest = [
        {
            "index": r["index"],
            "claim": r["claim"],
            "load_bearing": r["load_bearing"],
            "disconfirming_evidence": r["disconfirming_evidence"],
            "required_repair": r["required_repair"],
        }
        for r in records
        if r["contested"]
    ]
    unverified = [
        {
            "index": r["index"],
            "claim": r["claim"],
            "load_bearing": r["load_bearing"],
            "reason": r.get("review_status") or "unreviewed",
        }
        for r in records
        if r["status"] == "unverified"
    ]

    merged["claims"] = records
    merged["accepted_claims"] = [
        {"claim": r["claim"], "evidence": r.get("evidence") or []}
        for r in records
        if r["status"] == "accepted"
    ]
    merged["contested_claims"] = contest
    merged["unverified_claims"] = unverified

    # The resolution criterion: every LOAD-BEARING claim non-contested AND
    # reviewed, and no deterministic check failed. An unreviewed load-bearing
    # claim cannot count as accepted — that is what makes the cap safe. A
    # contested claim that explicitly opted out of being load-bearing is
    # reported above and does not hold the item open.
    #
    # The deterministic clause is deliberately NOT scoped to load-bearing
    # claims: a missing file or an absent quote is an objective falsehood, not
    # an opinion, so declaring the claim incidental cannot wave it away. Only
    # the judge's *contention* is reversible by `load_bearing: false`.
    blocking = [
        r
        for r in records
        if not r["deterministic_ok"]
        or (r["load_bearing"] and (r["contested"] or not r["reviewed"]))
    ]
    if not blocking:
        verdict = "accepted"
    elif any(r["deterministic_ok"] and r["reviewed"] and not r["contested"] for r in records):
        verdict = "needs_followup"
    else:
        verdict = "rejected"

    # A judge that answered with the old whole-report shape still decides the
    # verdict, so a provider/prompt that has not learned the per-claim shape
    # keeps working rather than silently falling back to deterministic.
    legacy = _legacy_verdict(
        [r["legacy_verdict"] for r in reviews.values() if r.get("legacy_verdict")]
    )
    if legacy:
        verdict = legacy

    summaries = [r["summary"] for r in reviews.values() if r.get("summary")]
    if summaries:
        summary = summaries[0]
    else:
        summary = (
            f"Verification {verdict}: {len(records)} claim(s) — "
            f"{sum(1 for r in records if r['status'] == 'accepted')} accepted, "
            f"{len(contest)} contested, {len(unverified)} unverified, "
            f"{sum(1 for r in records if r['status'] == 'failed')} deterministic "
            f"failure(s)."
        )

    merged["verdict"] = verdict
    merged["summary_for_parent"] = summary
    merged["llm_verification"] = {
        "enabled": True,
        "reviewed_claims": len(reviews),
        "max_claim_reviews": cap,
        "errors": errors,
        "summary_for_parent": summary,
    }
    return merged


def _call_llm_claim_review(
    *,
    claim_text: str,
    evidence: List[Any],
    constraints: List[str],
    deterministic_evidence: List[Any],
    node_goal: str,
    objective: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    prompt = {
        "objective": objective,
        "node_goal": node_goal,
        "claim": claim_text,
        "evidence": evidence,
        "constraints": constraints,
        "deterministic_checks": deterministic_evidence,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict verifier for ONE claim from a long-horizon "
                "agent report. Judge only whether THIS claim is supported by the "
                "evidence shown. Do not redo the task and do not accept a claim "
                "without evidence. Return only JSON with keys: contested "
                "(boolean), disconfirming_evidence (array of short strings), "
                "required_repair (string)."
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
        max_tokens=int(cfg.get("max_tokens") or 500),
        timeout=float(cfg.get("timeout") or 30),
    )
    text = (extract_content_or_reasoning(response) or "").strip()
    return _parse_claim_review(text)


def _parse_claim_review(text: str) -> Dict[str, Any]:
    """Parse one claim review, accepting the legacy whole-report shape too."""
    data = _parse_json_object(text)
    if any(key in data for key in ("contested", "disconfirming_evidence", "required_repair")):
        return {
            "reviewed": True,
            "review_status": "reviewed",
            "contested": _as_bool(data.get("contested")),
            "disconfirming_evidence": _as_str_list(data.get("disconfirming_evidence")),
            "required_repair": str(data.get("required_repair") or "").strip(),
        }
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict in {"accepted", "rejected", "needs_followup"}:
        # Legacy shape: the judge answered about the whole report. Treat a
        # non-accepted verdict as contesting this claim, and keep its summary so
        # the aggregate still carries the judge's own words.
        disconfirming = _as_str_list(data.get("missing_evidence"))
        if not disconfirming:
            disconfirming = _as_str_list(data.get("rejected_claims"))
        return {
            "reviewed": True,
            "review_status": "reviewed",
            "contested": verdict != "accepted",
            "disconfirming_evidence": disconfirming,
            "required_repair": "",
            "legacy_verdict": verdict,
            "summary": str(data.get("summary_for_parent") or "").strip(),
        }
    raise ValueError(f"LLM claim review returned neither a contested flag nor a verdict: {data!r}")


def _applicable_constraints(
    evidence: List[Any], *, node_goal: str, objective: str
) -> List[str]:
    """Project the verifier's rules onto the evidence kinds this claim cites."""
    kinds: Set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        kinds.add(str(item.get("kind") or item.get("type") or "").strip().lower())

    constraints = [_CONSTRAINT_LIBRARY["evidence"], _CONSTRAINT_LIBRARY["scope"]]
    if node_goal:
        constraints.append(f"Node goal: {node_goal}")
    if objective:
        constraints.append(f"Board objective: {objective}")
    if kinds & {"file", "test"}:
        constraints.append(_CONSTRAINT_LIBRARY["file"])
        constraints.append(_CONSTRAINT_LIBRARY["quote"])
    for kind in ("command", "url", "observation"):
        if kind in kinds:
            constraints.append(_CONSTRAINT_LIBRARY[kind])
    return constraints


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


def _is_load_bearing(claim: Dict[str, Any]) -> bool:
    """A claim is load-bearing unless it explicitly opts out.

    The rule defaults to load-bearing because that is the safe direction: a
    claim that matters must not become non-blocking by omission, and a model
    writing a terse report will not remember to tag it. Only a deliberate
    ``load_bearing: false`` (or its obvious synonyms) makes a claim incidental
    enough that disputing it cannot hold the item open.
    """
    value = claim.get("load_bearing")
    if value is None:
        value = claim.get("loadBearing")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return True
        return text not in {"false", "no", "0", "off", "none"}
    return True


def _record_status(record: Dict[str, Any], *, llm_enabled: bool) -> str:
    if not record["deterministic_ok"]:
        return "failed"
    if record["contested"]:
        return "contested"
    if llm_enabled and not record["reviewed"]:
        # Unverified is its own state on purpose: it is not a pass and it is not
        # a failure, and it must never be silently folded into either.
        return "unverified"
    return "accepted"


def _claim_record(
    index: int,
    text: str,
    *,
    load_bearing: bool,
    checks: List[Dict[str, Any]],
    bad: List[Dict[str, Any]],
) -> Dict[str, Any]:
    record = {
        "index": index,
        "claim": text,
        "load_bearing": load_bearing,
        "deterministic_ok": not bad,
        "reviewed": False,
        "contested": False,
        "disconfirming_evidence": [_disconfirming_from_check(item) for item in bad],
        "required_repair": _repair_hint(bad),
        "evidence": checks,
    }
    record["status"] = _record_status(record, llm_enabled=False)
    return record


def _disconfirming_from_check(check: Dict[str, Any]) -> Dict[str, Any]:
    entry = {
        "kind": str(check.get("kind") or ""),
        "ref": str(check.get("ref") or ""),
        "reason": str(check.get("reason") or ""),
    }
    if check.get("searched_for"):
        entry["searched_for"] = str(check["searched_for"])
    if check.get("line"):
        entry["line"] = check["line"]
    return entry


def _repair_hint(bad: List[Dict[str, Any]]) -> str:
    if not bad:
        return ""
    return f"Evidence check failed: {bad[0].get('reason') or 'unsupported evidence'}."


def _max_claim_reviews(cfg: Dict[str, Any]) -> int:
    try:
        cap = int((cfg or {}).get("max_claim_reviews"))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CLAIM_REVIEWS
    return cap if cap > 0 else DEFAULT_MAX_CLAIM_REVIEWS


def _legacy_verdict(verdicts: List[str]) -> Optional[str]:
    if not verdicts:
        return None
    if all(verdict == "accepted" for verdict in verdicts):
        return "accepted"
    if "needs_followup" in verdicts:
        return "needs_followup"
    return "rejected"


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value:
        return [str(value)]
    return []


def _check_evidence(item: Any, *, root: Optional[Path]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"ok": False, "reason": "evidence is not an object"}
    kind = str(item.get("kind") or item.get("type") or "").strip().lower()
    ref = str(item.get("ref") or "").strip()
    quote = str(item.get("quote") or item.get("output") or "").strip()

    if kind == "file":
        return _check_file_evidence(kind, ref, quote, root=root)
    if kind == "test":
        return _check_test_evidence(ref, quote, item, root=root)
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


def _check_file_evidence(
    kind: str, ref: str, quote: str, *, root: Optional[Path]
) -> Dict[str, Any]:
    if not ref:
        return {"ok": False, "kind": kind, "reason": "file evidence missing ref"}
    path_ref, line = _parse_file_ref(ref)
    path = _resolve_path(path_ref, root)
    if path is None or not path.exists():
        return {"ok": False, "kind": kind, "ref": ref, "reason": "file does not exist"}
    if not path.is_file():
        return {
            "ok": True,
            "kind": kind,
            "ref": ref,
            "weak": True,
            "reason": "path exists but is not a file (weak evidence)",
        }
    return _check_file_content(kind, ref, path_ref, line, quote, path)


def _check_test_evidence(
    ref: str, quote: str, item: Dict[str, Any], *, root: Optional[Path]
) -> Dict[str, Any]:
    """Test evidence: a ref that names a real test file is checked like a file."""
    if not ref:
        return {"ok": False, "kind": "test", "reason": "test evidence missing ref"}
    path_ref, line = _parse_file_ref(ref)
    path = _resolve_path(path_ref, root)
    if path is not None and path.is_file():
        return _check_file_content("test", ref, path_ref, line, quote, path)
    if path is not None and path.exists():
        return _check_file_evidence("test", ref, quote, root=root)
    if path_ref.endswith(".py") or "/" in path_ref or "\\" in path_ref:
        # Looks like a path and is not there: the same failure a file ref gets,
        # rather than quietly downgrading to a free-form identifier.
        return {"ok": False, "kind": "test", "ref": ref, "reason": "file does not exist"}
    has_result = bool(quote or item.get("exit_code") is not None)
    return {
        "ok": has_result,
        "kind": "test",
        "ref": ref,
        "reason": "test evidence has an identifier and result"
        if has_result
        else "test evidence needs a quote/output or exit_code",
    }


def _check_file_content(
    kind: str,
    ref: str,
    path_ref: str,
    line: Optional[int],
    quote: str,
    path: Path,
) -> Dict[str, Any]:
    """Content-level check: a cited quote must occur in the cited file.

    File existence alone cannot support a claim about what a file says — a
    fabricated quote passed every check this verifier used to run.
    """
    if not quote:
        # A bare path proves only that the file exists, never what it contains.
        # Recorded weak rather than failed so existence-only reports keep their
        # existing verdict.
        return {
            "ok": True,
            "kind": kind,
            "ref": ref,
            "line": line,
            "weak": True,
            "reason": "file exists; no quote cited to check (weak evidence)",
        }
    hit, searched = _quote_hit(path, quote, line)
    if hit:
        return {
            "ok": True,
            "kind": kind,
            "ref": ref,
            "line": line,
            "reason": "cited quote found in the file"
            + (f" near line {line}" if line else ""),
        }
    where = path_ref + (f" near line {line}" if line else "")
    return {
        "ok": False,
        "kind": kind,
        "ref": ref,
        "line": line,
        "searched_for": searched,
        "reason": f"cited quote not found in {where}",
    }


def _quote_hit(path: Path, quote: str, line: Optional[int]) -> Tuple[bool, str]:
    """Whitespace-normalised search for the quote, optionally near a line."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, _normalize_ws(quote)
    variants = _quote_variants(quote)
    if not variants:
        return False, _normalize_ws(quote)
    if line is None:
        haystack = _normalize_ws(content)
    else:
        lines = content.splitlines()
        start = max(0, line - 1 - _QUOTE_WINDOW_LINES)
        end = min(len(lines), line + _QUOTE_WINDOW_LINES)
        haystack = _normalize_ws("\n".join(lines[start:end]))
    return any(variant in haystack for variant in variants), variants[0]


def _quote_variants(quote: str) -> List[str]:
    """The quote, plus its form with an explicit truncation marker removed.

    An excerpt that ends in ``...`` is honest about being partial; requiring the
    marker itself to appear in the file would fail a truthful quote.
    """
    variants = [quote]
    stripped = quote.rstrip()
    for marker in _TRUNCATION_MARKERS:
        if stripped.endswith(marker):
            variants.append(stripped[: -len(marker)].rstrip())
    normalized: List[str] = []
    for variant in variants:
        text = _normalize_ws(variant)
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _normalize_ws(text: Any) -> str:
    return " ".join(str(text).split())


def _parse_file_ref(ref: str) -> Tuple[str, Optional[int]]:
    """Split ``path`` / ``path:line`` / ``path:line:col`` / ``path::node``."""
    text = ref.strip()
    if "::" in text:
        # pytest node id: the anchor is a test name, not a line number.
        return text.split("::", 1)[0].strip(), None
    match = _LINE_ANCHOR_RE.match(text)
    if match:
        return match.group("path").strip(), int(match.group("line"))
    return text, None


def _resolve_path(raw: str, root: Optional[Path]) -> Optional[Path]:
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() and root is not None:
        path = root / path
    return path


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
        "claims": [],
    }
