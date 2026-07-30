"""Versioned TOEFL 2026 configuration, scoring, and practice planning."""

from __future__ import annotations

import copy
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "exam_profiles"
    / "toefl_2026"
    / "profile.json"
)
SECTION_NAMES = ("reading", "listening", "speaking", "writing")


class ExamService:
    def __init__(self, profile_path: str | Path = PROFILE_PATH):
        self.profile_path = Path(profile_path)
        self._profile = self._load_profile()

    def profile(self) -> dict[str, Any]:
        return copy.deepcopy(self._profile)

    def calculate_score(self, section_scores: dict[str, str | float]) -> dict[str, Any]:
        if set(section_scores) != set(SECTION_NAMES):
            raise ValueError(
                "section_scores must contain reading, listening, speaking, and writing"
            )
        scale = self._profile["score_scale"]
        minimum = Decimal(scale["minimum"])
        maximum = Decimal(scale["maximum"])
        step = Decimal(scale["step"])
        parsed: dict[str, Decimal] = {}
        for section in SECTION_NAMES:
            try:
                value = Decimal(str(section_scores[section]))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"invalid {section} score") from exc
            if not value.is_finite() or value < minimum or value > maximum:
                raise ValueError(
                    f"{section} score must be between {minimum} and {maximum}"
                )
            if (value - minimum) % step != 0:
                raise ValueError(f"{section} score must use {step} increments")
            parsed[section] = value
        mean = sum(parsed.values(), Decimal("0")) / Decimal(len(SECTION_NAMES))
        overall = (mean / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
        overall_text = self._decimal_text(overall)
        legacy = next(
            (
                item
                for item in self._profile["legacy_total_comparison"]
                if item["band"] == overall_text
            ),
            None,
        )
        return {
            "exam_id": self._profile["exam_id"],
            "profile_version": self._profile["profile_version"],
            "section_scores": {
                section: self._decimal_text(parsed[section])
                for section in SECTION_NAMES
            },
            "section_mean": self._plain_decimal_text(mean),
            "overall": overall_text,
            "legacy_comparable_total_range": (
                {"minimum": legacy["minimum"], "maximum": legacy["maximum"]}
                if legacy
                else None
            ),
            "legacy_mapping_kind": "comparison_range_not_exact_conversion",
            "disclaimer": self._profile["disclaimer"],
        }

    def practice_plan(
        self,
        *,
        minutes: int = 30,
        sections: Iterable[str] = ("reading", "writing"),
    ) -> dict[str, Any]:
        if not 5 <= minutes <= 180:
            raise ValueError("minutes must be between 5 and 180")
        requested = list(dict.fromkeys(str(section).strip() for section in sections))
        if not requested:
            raise ValueError("at least one section is required")
        tasks_by_section: dict[str, list[dict[str, Any]]] = {}
        for section in requested:
            definition = self._profile["sections"].get(section)
            if not definition:
                raise ValueError(f"unknown TOEFL section: {section}")
            if not definition.get("practice_supported"):
                raise ValueError(
                    f"{section} practice is not implemented in this MVP"
                )
            tasks_by_section[section] = definition["task_types"]
        task_pool = [
            (section, tasks_by_section[section][task_index])
            for task_index in range(
                max(len(tasks) for tasks in tasks_by_section.values())
            )
            for section in requested
            if task_index < len(tasks_by_section[section])
        ]
        plan: list[dict[str, Any]] = []
        used = 0
        sequence = 1
        while task_pool:
            added = False
            for section, task in task_pool:
                duration = int(task["practice_minutes"])
                if used + duration > minutes:
                    continue
                plan.append(
                    {
                        "sequence": sequence,
                        "section": section,
                        "task_type": task["id"],
                        "task_name": task["name"],
                        "minutes": duration,
                        "response_mode": task["response_mode"],
                    }
                )
                sequence += 1
                used += duration
                added = True
            if not added:
                break
        return {
            "exam_id": self._profile["exam_id"],
            "profile_version": self._profile["profile_version"],
            "requested_minutes": minutes,
            "scheduled_minutes": used,
            "remaining_minutes": minutes - used,
            "sections": requested,
            "tasks": plan,
            "adaptive_simulation": False,
            "disclaimer": self._profile["disclaimer"],
        }

    def _load_profile(self) -> dict[str, Any]:
        try:
            profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load exam profile: {exc}") from exc
        if profile.get("exam_id") != "toefl_ibt":
            raise ValueError("exam profile must identify toefl_ibt")
        if profile.get("status") != "active":
            raise ValueError("exam profile is not active")
        if set(profile.get("sections", {})) != set(SECTION_NAMES):
            raise ValueError("exam profile must define all four TOEFL sections")
        scale = profile.get("score_scale", {})
        score_method = "mean_then_round_to_nearest_half_band_half_up"
        if scale.get("overall_method") != score_method:
            raise ValueError("unsupported TOEFL overall score method")
        if not profile.get("sources"):
            raise ValueError("exam profile must preserve official sources")
        return profile

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.0'))}"

    @staticmethod
    def _plain_decimal_text(value: Decimal) -> str:
        return format(value, "f")
