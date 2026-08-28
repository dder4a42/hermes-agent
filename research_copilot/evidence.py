"""Review-gated evidence ledger and profile update proposals."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

import yaml

from .library import LibraryRepository
from .preferences import ResearchPreferences


QUALITY_WEIGHTS = {
    "primary": 1.0,
    "official": 0.9,
    "secondary": 0.7,
    "community": 0.5,
    "unknown": 0.4,
}


def profile_revision(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_evidence_scope(
    preferences: ResearchPreferences,
    *,
    belief_id: str | None,
    prompt_id: str | None,
) -> str:
    agenda_ids: set[str] = set()
    if belief_id:
        agenda_ids.add(preferences.belief(belief_id).agenda_id)
    if prompt_id:
        agenda_id, _prompt = preferences.prompt(prompt_id)
        agenda_ids.add(agenda_id)
    if not agenda_ids:
        raise ValueError("Evidence must reference --belief or --prompt")
    if len(agenda_ids) != 1:
        raise ValueError("Evidence belief and prompt must belong to the same agenda")
    return next(iter(agenda_ids))


def create_evidence(
    repository: LibraryRepository,
    preferences: ResearchPreferences,
    *,
    item_id: str,
    belief_id: str | None,
    prompt_id: str | None,
    relation: str,
    claim_type: str,
    strength: float,
    source_quality: str,
    claim: str,
    rationale: str,
    profile_path: str | Path,
    created_at: datetime | None = None,
) -> str:
    agenda_id = resolve_evidence_scope(
        preferences, belief_id=belief_id, prompt_id=prompt_id,
    )
    return repository.record_evidence(
        item_id,
        created_at=created_at or datetime.now(timezone.utc),
        agenda_id=agenda_id,
        belief_id=belief_id,
        prompt_id=prompt_id,
        relation=relation,
        claim_type=claim_type,
        strength=strength,
        source_quality=source_quality,
        claim=claim,
        rationale=rationale,
        profile_revision=profile_revision(profile_path),
    )


def _suggested_confidence(current: float, evidence: list[dict]) -> tuple[float, float]:
    delta = 0.0
    for record in evidence:
        weight = QUALITY_WEIGHTS.get(str(record["source_quality"]), 0.4)
        strength = float(record["strength"])
        if record["relation"] == "supports":
            delta += 0.04 * strength * weight
        elif record["relation"] == "challenges":
            delta -= 0.07 * strength * weight
    delta = max(-0.1, min(0.1, delta))
    suggested = max(0.0, min(1.0, current + delta))
    return round(suggested, 4), round(delta, 4)


def build_profile_update_proposal(
    repository: LibraryRepository,
    preferences: ResearchPreferences,
    *,
    profile_path: str | Path,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    now = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    revision = profile_revision(profile_path)
    accepted = repository.list_evidence(review_status="accepted")
    by_belief: dict[str, list[dict]] = {}
    by_prompt: dict[str, list[dict]] = {}
    orphaned_evidence: list[dict[str, Any]] = []
    belief_ids = {belief.id for belief in preferences.beliefs}
    prompt_ids = {
        prompt.id
        for agenda in preferences.agenda
        for prompt in (*agenda.open_question_refs, *agenda.knowledge_gap_refs)
    }
    for record in accepted:
        if record.get("belief_id"):
            belief_id = str(record["belief_id"])
            if belief_id in belief_ids:
                by_belief.setdefault(belief_id, []).append(record)
            else:
                orphaned_evidence.append(_orphaned_target(record, "belief", belief_id))
        if record.get("prompt_id"):
            prompt_id = str(record["prompt_id"])
            if prompt_id in prompt_ids:
                by_prompt.setdefault(prompt_id, []).append(record)
            else:
                orphaned_evidence.append(_orphaned_target(record, "prompt", prompt_id))

    belief_updates = []
    for belief in preferences.beliefs:
        records = by_belief.get(belief.id, [])
        if not records:
            continue
        suggested, delta = _suggested_confidence(belief.confidence, records)
        belief_updates.append({
            "belief_id": belief.id,
            "agenda_id": belief.agenda_id,
            "statement": belief.statement,
            "current_confidence": belief.confidence,
            "suggested_confidence": suggested,
            "suggested_delta": delta,
            "evidence": [_proposal_evidence(record, revision) for record in records],
        })

    prompt_updates = []
    for prompt_id, records in sorted(by_prompt.items()):
        agenda_id, prompt = preferences.prompt(prompt_id)
        prompt_updates.append({
            "prompt_id": prompt_id,
            "agenda_id": agenda_id,
            "prompt": prompt.text,
            "evidence": [_proposal_evidence(record, revision) for record in records],
        })

    return {
        "schema_version": 1,
        "proposal_id": f"profile-update-{now.strftime('%Y%m%dT%H%M%SZ')}",
        "created_at": now.isoformat(),
        "profile_revision": revision,
        "status": "pending_user_confirmation",
        "policy": {
            "mutates_profile": False,
            "confidence_heuristic": (
                "supports=+0.04*strength*quality; "
                "challenges=-0.07*strength*quality; batch delta capped at +/-0.10"
            ),
        },
        "belief_updates": belief_updates,
        "prompt_updates": prompt_updates,
        "orphaned_evidence": orphaned_evidence,
    }


def _proposal_evidence(record: dict, current_revision: str) -> dict[str, Any]:
    return {
        "evidence_id": record["id"],
        "item_id": record["item_id"],
        "title": record["title"],
        "url": record["url"],
        "relation": record["relation"],
        "claim_type": record["claim_type"],
        "strength": float(record["strength"]),
        "source_quality": record["source_quality"],
        "claim": record["claim"],
        "rationale": record["rationale"],
        "profile_revision": record["profile_revision"],
        "profile_revision_stale": record["profile_revision"] != current_revision,
    }


def _orphaned_target(record: dict, target_kind: str, target_id: str) -> dict[str, Any]:
    return {
        "evidence_id": record["id"],
        "item_id": record["item_id"],
        "title": record["title"],
        "target_kind": target_kind,
        "target_id": target_id,
        "reason": "target_missing_from_current_profile",
    }


def write_profile_update_proposal(
    destination_dir: str | Path,
    proposal: dict[str, Any],
) -> Path:
    from utils import atomic_yaml_write

    directory = Path(destination_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{proposal['proposal_id']}.yaml"
    atomic_yaml_write(destination, proposal, sort_keys=False)
    return destination


def render_proposal(proposal: dict[str, Any]) -> str:
    return yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True)
