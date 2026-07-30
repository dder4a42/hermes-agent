"""Proactive, source-grounded reading lessons with optional LLM adaptation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .database import LearningDatabase
from .service import iso, utc_now


PROMPT_VERSION = "reading-tutor-v1"
QUESTION_TYPES = frozenset({"main_idea", "detail", "inference", "function"})

SEEDS: tuple[dict[str, Any], ...] = (
    {
        "id": "openstax-scientific-models",
        "topic": "science",
        "title": "Models, evidence, and revision",
        "level": "B1",
        "source": {
            "title": "OpenStax Biology 2e",
            "publisher": "OpenStax",
            "url": "https://openstax.org/details/books/biology-2e",
            "license": "CC BY-NC-SA 4.0",
        },
        "passage": (
            "Scientists often use models to explain systems that are too large, small, "
            "or complex to observe directly. A model may be a diagram, a set of equations, "
            "or a simplified description. Its value does not depend on being a perfect copy "
            "of reality. Instead, a useful model makes relationships clear and produces "
            "predictions that can be compared with evidence. When new observations disagree "
            "with a prediction, researchers examine both the evidence and the assumptions of "
            "the model. They may correct a measurement, change one assumption, or replace the "
            "model entirely. This process is not a sign that science has failed. Revision is "
            "one of the ways scientific knowledge becomes more reliable over time."
        ),
        "questions": [
            {
                "id": "q1",
                "type": "main_idea",
                "prompt": "What is the main purpose of the passage?",
                "answer": "To explain how evidence is used to evaluate and revise scientific models.",
            },
            {
                "id": "q2",
                "type": "inference",
                "prompt": "Why is a simplified model still useful?",
                "answer": "It can make important relationships clear and produce testable predictions.",
            },
        ],
        "writing_prompt": "In 50–80 words, explain why revising a model can strengthen science.",
    },
    {
        "id": "mit-feedback-systems",
        "topic": "computing",
        "title": "Why feedback changes a system",
        "level": "B1",
        "source": {
            "title": "MIT OpenCourseWare: Feedback Systems",
            "publisher": "MIT OpenCourseWare",
            "url": "https://ocw.mit.edu/courses/6-302-feedback-systems-spring-2007/",
            "license": "CC BY-NC-SA 4.0",
        },
        "passage": (
            "A system uses feedback when information about its output influences what it does "
            "next. A simple thermostat provides a familiar example. It measures the room "
            "temperature, compares that measurement with a target, and changes the heater's "
            "behavior. Feedback also appears in software and machine learning. A search system "
            "may observe which results people select and use that signal to improve later "
            "rankings. However, feedback is not automatically beneficial. If the signal is "
            "delayed, incomplete, or biased, the system may repeatedly make the wrong adjustment. "
            "Designers therefore need to ask what is being measured, how quickly the system "
            "responds, and whether the response remains stable under unusual conditions."
        ),
        "questions": [
            {
                "id": "q1",
                "type": "main_idea",
                "prompt": "What common principle connects the thermostat and search examples?",
                "answer": "Both use information about output to change later behavior.",
            },
            {
                "id": "q2",
                "type": "detail",
                "prompt": "Name two properties that can make a feedback signal harmful.",
                "answer": "It may be delayed, incomplete, or biased.",
            },
        ],
        "writing_prompt": "In 50–80 words, describe one feedback system and one risk it faces.",
    },
    {
        "id": "ncbi-evidence-synthesis",
        "topic": "health",
        "title": "Combining evidence carefully",
        "level": "B2",
        "source": {
            "title": "NCBI Bookshelf",
            "publisher": "National Center for Biotechnology Information",
            "url": "https://www.ncbi.nlm.nih.gov/books/",
            "license": "Source-specific; linked for factual grounding",
        },
        "passage": (
            "One study rarely settles a difficult health question. Different studies may use "
            "different populations, measurements, or research designs, so their conclusions can "
            "vary. Evidence synthesis is a structured attempt to understand the larger pattern. "
            "Researchers first define a precise question and a transparent method for finding "
            "relevant studies. They then assess the quality of each study rather than treating "
            "every result as equally reliable. If the studies are sufficiently similar, their "
            "results may be combined statistically. If they differ in important ways, a careful "
            "review explains those differences instead of hiding them in a single number. The "
            "goal is not to create certainty, but to state what the available evidence supports, "
            "where uncertainty remains, and what future research should address."
        ),
        "questions": [
            {
                "id": "q1",
                "type": "main_idea",
                "prompt": "What is evidence synthesis designed to do?",
                "answer": "It evaluates multiple studies to identify what the overall evidence supports.",
            },
            {
                "id": "q2",
                "type": "function",
                "prompt": "Why does the passage mention differences among studies?",
                "answer": "To show why results cannot always be combined or weighted equally.",
            },
        ],
        "writing_prompt": "In 60–90 words, explain why several studies may be stronger than one study.",
    },
)


class HermesReadingGenerator:
    """Call the configured Hermes model without expanding the core tool surface."""

    name = "hermes-oneshot"

    def __init__(self, executable: str = "hermes", timeout_seconds: int = 90):
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def __call__(self, payload: dict) -> dict:
        prompt = _generation_prompt(payload)
        completed = subprocess.run(
            [self.executable, "-z", prompt, "--ignore-rules"],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Hermes reading generation failed")
        return _parse_json_object(completed.stdout)


class ReadingTutorService:
    def __init__(
        self,
        database: LearningDatabase,
        *,
        generator: Callable[[dict], dict] | None = None,
    ):
        self.database = database
        self.database.initialize()
        self.generator = generator

    def today(
        self,
        *,
        level: str = "B1",
        minutes: int = 10,
        topic: str = "science",
        collection_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        level = level.strip().upper()
        topic = topic.strip().casefold()
        if level not in {"A2", "B1", "B2", "C1"}:
            raise ValueError("level must be A2, B1, B2, or C1")
        if minutes not in {5, 10, 15, 30}:
            raise ValueError("minutes must be 5, 10, 15, or 30")
        now = now or utc_now()
        seed = self._select_seed(topic, now)
        targets = self._target_items(collection_id, limit=4)
        request = {
            "date": now.date().isoformat(),
            "level": level,
            "minutes": minutes,
            "topic": topic,
            "collection_id": collection_id,
            "seed_id": seed["id"],
            "target_sense_ids": [item["sense_id"] for item in targets],
            "prompt_version": PROMPT_VERSION,
        }
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cached = self._cached(request_hash)
        if cached:
            cached["cached"] = True
            return cached

        generation_input = {
            "lesson": {"level": level, "minutes": minutes, "topic": topic},
            "source_seed": seed,
            "learner": {"target_items": targets},
            "constraints": {
                "target_word_count": {5: 100, 10: 180, 15: 260, 30: 420}[minutes],
                "maximum_new_language_points": 4,
                "preserve_source_facts": True,
            },
        }
        status = "generated"
        source_mode = "source_adapted"
        try:
            generated = self.generator(generation_input) if self.generator else None
            if generated is None:
                raise RuntimeError("no generator configured")
            lesson = self._validate_generated(generated, seed, targets, level)
            generator_name = getattr(self.generator, "name", "callable")
        except Exception:
            lesson = self._fallback(seed, targets)
            status = "fallback"
            source_mode = "curated_seed"
            generator_name = getattr(self.generator, "name", "unavailable")

        result = {
            "lesson_id": str(uuid.uuid4()),
            **lesson,
            "source": seed["source"],
            "seed_id": seed["id"],
            "source_mode": source_mode,
            "generation_status": status,
            "prompt_version": PROMPT_VERSION,
            "cached": False,
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO reading_tutor_sessions(
                    id, request_hash, seed_id, request_json, result_json,
                    source_mode, generation_status, generator_name,
                    prompt_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["lesson_id"], request_hash, seed["id"],
                    json.dumps(request, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    source_mode, status, generator_name, PROMPT_VERSION, iso(now),
                ),
            )
        return result

    def _cached(self, request_hash: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM reading_tutor_sessions WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    @staticmethod
    def _select_seed(topic: str, now: datetime) -> dict:
        matches = [seed for seed in SEEDS if seed["topic"] == topic] or list(SEEDS)
        index = int(hashlib.sha256(now.date().isoformat().encode()).hexdigest(), 16)
        return matches[index % len(matches)]

    def _target_items(self, collection_id: str | None, *, limit: int) -> list[dict]:
        collection_join = ""
        params: list[Any] = []
        if collection_id:
            collection_join = (
                "JOIN word_sense_collections wsc ON wsc.sense_id = ws.id "
                "AND wsc.collection_id = ?"
            )
            params.append(collection_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT ws.id AS sense_id, ws.lemma, ws.part_of_speech,
                       ws.definition_en, ws.definition_zh,
                       COALESCE(uks.recognition_score, 0.0) AS recognition_score,
                       COALESCE(uks.recognition_evidence_count, 0) AS evidence_count
                FROM word_senses ws
                {collection_join}
                LEFT JOIN user_knowledge_states uks ON uks.sense_id = ws.id
                WHERE COALESCE(uks.recognition_score, 0.0) < 0.7
                ORDER BY evidence_count DESC, recognition_score, ws.frequency_rank, ws.id
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_generated(
        generated: dict, seed: dict, targets: list[dict], requested_level: str
    ) -> dict:
        if not isinstance(generated, dict):
            raise ValueError("generated lesson must be an object")
        title = str(generated.get("title", "")).strip()
        passage = str(generated.get("passage", "")).strip()
        why = str(generated.get("why_this_passage", "")).strip()
        writing_prompt = str(generated.get("writing_prompt", "")).strip()
        questions = generated.get("questions")
        if not title or len(passage.split()) < 7 or not why or not writing_prompt:
            raise ValueError("generated lesson is incomplete")
        if not isinstance(questions, list) or not questions:
            raise ValueError("generated lesson needs questions")
        clean_questions = []
        for index, question in enumerate(questions[:5], start=1):
            question_type = str(question.get("type", "")).strip()
            prompt = str(question.get("prompt", "")).strip()
            answer = str(question.get("answer", "")).strip()
            if question_type not in QUESTION_TYPES or not prompt or not answer:
                raise ValueError("invalid reading question")
            clean_questions.append(
                {"id": str(question.get("id") or f"q{index}"), "type": question_type,
                 "prompt": prompt, "answer": answer}
            )
        allowed = {item["lemma"].casefold(): item for item in targets}
        requested = generated.get("target_lemmas") or []
        selected = [allowed[lemma.casefold()] for lemma in requested if isinstance(lemma, str) and lemma.casefold() in allowed]
        return {
            "title": title,
            "passage": passage,
            "level": str(generated.get("level") or requested_level).upper(),
            "topic": seed["topic"],
            "why_this_passage": why,
            "target_items": selected,
            "questions": clean_questions,
            "writing_prompt": writing_prompt,
        }

    @staticmethod
    def _fallback(seed: dict, targets: list[dict]) -> dict:
        return {
            "title": seed["title"], "passage": seed["passage"],
            "level": seed["level"], "topic": seed["topic"],
            "why_this_passage": "这是一篇经过筛选的说明文，可用于练习主旨、证据和摘要。",
            "target_items": targets[:3], "questions": seed["questions"],
            "writing_prompt": seed["writing_prompt"],
        }


def _generation_prompt(payload: dict) -> str:
    return (
        "You are a private English reading tutor. Return one JSON object only, with keys "
        "title, passage, level, why_this_passage, target_lemmas, questions, and "
        "writing_prompt. Each question needs id, type (main_idea/detail/inference/function), "
        "prompt, and answer. Adapt wording and syntax to the learner, preserve the source "
        "facts, and never claim the adapted passage is a quotation. Input:\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value
