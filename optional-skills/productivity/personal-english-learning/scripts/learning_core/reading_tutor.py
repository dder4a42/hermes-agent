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


PROMPT_VERSION = "reading-tutor-v2"
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
    {
        "id": "openstax-ecosystem-resilience",
        "topic": "science",
        "title": "How ecosystems respond to disturbance",
        "level": "B2",
        "source": {
            "title": "OpenStax Biology 2e",
            "publisher": "OpenStax",
            "url": "https://openstax.org/details/books/biology-2e",
            "license": "CC BY 4.0",
        },
        "passage": (
            "An ecosystem is not static. Fires, storms, droughts, and human activity can alter "
            "the organisms and resources within it. Ecologists use the term resilience for an "
            "ecosystem's ability to recover after such a disturbance. Recovery does not always "
            "mean returning to exactly the same condition. Some species may become less common "
            "while others occupy newly available space. Diversity can support resilience because "
            "different species often respond to stress in different ways. Yet diversity alone is "
            "not a guarantee: a disturbance may be too frequent or severe for normal recovery. "
            "Researchers therefore study both the structure of an ecosystem and the history of "
            "the pressures acting on it before deciding how it should be protected."
        ),
        "questions": [
            {"id": "q1", "type": "main_idea", "prompt": "What does the passage explain about ecosystem resilience?", "answer": "It explains recovery after disturbance and the factors that can support or limit it."},
            {"id": "q2", "type": "inference", "prompt": "Why may a recovered ecosystem differ from its earlier state?", "answer": "Species can respond differently and occupy newly available space."},
        ],
        "writing_prompt": "In 60–90 words, explain why recovery does not always mean restoration to an identical state.",
    },
    {
        "id": "openstax-memory-retrieval",
        "topic": "psychology",
        "title": "Why retrieving a memory changes learning",
        "level": "B1",
        "source": {
            "title": "OpenStax Psychology 2e",
            "publisher": "OpenStax",
            "url": "https://openstax.org/details/books/psychology-2e",
            "license": "CC BY 4.0",
        },
        "passage": (
            "Reading the same notes several times can create a feeling of familiarity, but "
            "familiarity is not the same as being able to recall an idea. Retrieval practice "
            "requires a learner to bring information to mind without first seeing the answer. "
            "A short quiz, a blank-page summary, or an attempt to explain a concept can all serve "
            "this purpose. The attempt may feel difficult, and mistakes are common, yet the act of "
            "retrieval helps strengthen access to the memory. Feedback remains important because "
            "it corrects errors before they become stable. Effective study therefore alternates "
            "between recalling information and checking it, rather than treating repeated reading "
            "as the only sign that learning has occurred."
        ),
        "questions": [
            {"id": "q1", "type": "main_idea", "prompt": "How does retrieval practice differ from repeated reading?", "answer": "It asks learners to recall information before seeing the answer."},
            {"id": "q2", "type": "function", "prompt": "Why does the passage mention feedback?", "answer": "Feedback corrects errors made during retrieval practice."},
        ],
        "writing_prompt": "In 50–80 words, compare rereading with retrieval practice.",
    },
    {
        "id": "openstax-cultural-norms",
        "topic": "society",
        "title": "How norms guide ordinary behavior",
        "level": "B1",
        "source": {
            "title": "Introduction to Sociology 3e",
            "publisher": "OpenStax",
            "url": "https://openstax.org/details/books/introduction-sociology-3e",
            "license": "CC BY 4.0",
        },
        "passage": (
            "People learn many social rules without receiving a written list of them. These "
            "shared expectations, often called norms, shape ordinary actions such as taking turns, "
            "choosing clothing, or speaking to a stranger. Some norms are supported by formal laws, "
            "whereas others are enforced through approval, embarrassment, or exclusion. Because "
            "norms vary between communities and change over time, behavior that seems natural in "
            "one setting may appear unusual in another. Learning a new culture therefore involves "
            "more than translating words. A learner must also notice which actions a group rewards, "
            "which it discourages, and how strongly people react when an expectation is broken."
        ),
        "questions": [
            {"id": "q1", "type": "main_idea", "prompt": "What role do social norms play?", "answer": "They create shared expectations that guide behavior."},
            {"id": "q2", "type": "inference", "prompt": "Why can translation alone be insufficient in a new culture?", "answer": "A learner also needs to understand unwritten expectations and reactions."},
        ],
        "writing_prompt": "In 50–80 words, describe one unwritten norm and how people enforce it.",
    },
    {
        "id": "openstax-trade-networks",
        "topic": "history",
        "title": "What moved through ancient trade networks",
        "level": "B2",
        "source": {
            "title": "World History Volume 2",
            "publisher": "OpenStax",
            "url": "https://openstax.org/details/books/world-history-volume-2",
            "license": "CC BY 4.0",
        },
        "passage": (
            "Long-distance trade routes carried more than valuable goods. Merchants, sailors, "
            "pilgrims, and diplomats also transported stories, technologies, artistic styles, and "
            "religious ideas. A product could pass through several communities before reaching its "
            "final buyer, so participants did not need to travel across an entire network themselves. "
            "The same connections that encouraged exchange could also spread disease or intensify "
            "competition over ports and roads. Historians therefore examine coins, shipwrecks, "
            "letters, and borrowed words to reconstruct how regions influenced one another. These "
            "sources reveal trade not as a simple movement between two places, but as a chain of "
            "relationships that changed both local economies and cultural life."
        ),
        "questions": [
            {"id": "q1", "type": "main_idea", "prompt": "Why were trade networks historically important beyond commerce?", "answer": "They also transmitted ideas, technologies, culture, and disease."},
            {"id": "q2", "type": "detail", "prompt": "What evidence can historians use to reconstruct trade networks?", "answer": "Coins, shipwrecks, letters, and borrowed words."},
        ],
        "writing_prompt": "In 60–90 words, explain one benefit and one risk of a trade network.",
    },
    {
        "id": "openstax-opportunity-cost",
        "topic": "economics",
        "title": "The cost hidden inside a choice",
        "level": "B1",
        "source": {
            "title": "Principles of Economics 3e",
            "publisher": "OpenStax",
            "url": "https://openstax.org/details/books/principles-economics-3e",
            "license": "CC BY 4.0",
        },
        "passage": (
            "Every choice uses resources that could have served another purpose. Economists call "
            "the value of the best alternative that is given up the opportunity cost. The idea "
            "includes money, but it is not limited to money. A free public lecture may still have "
            "a cost if attending it means missing work or giving up time with friends. Opportunity "
            "cost also depends on the alternatives available to a particular person, so two people "
            "can make the same visible choice while giving up very different things. Thinking in "
            "these terms does not determine which choice is morally right. It simply makes the "
            "trade-off explicit and helps explain why a decision that appears inexpensive may carry "
            "a significant hidden cost."
        ),
        "questions": [
            {"id": "q1", "type": "main_idea", "prompt": "What is opportunity cost?", "answer": "It is the value of the best alternative given up by a choice."},
            {"id": "q2", "type": "inference", "prompt": "Why can a free event still have an opportunity cost?", "answer": "It may require giving up time or another valuable activity."},
        ],
        "writing_prompt": "In 50–80 words, explain the opportunity cost of one recent choice.",
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
        topic: str = "mixed",
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
        recent_lessons = self._recent_lessons(now)
        seed = self._select_seed(
            topic,
            now,
            {lesson["seed_id"] for lesson in recent_lessons},
        )
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
            "recent_lessons": recent_lessons,
            "constraints": {
                "target_word_count": {5: 100, 10: 180, 15: 260, 30: 420}[minutes],
                "maximum_new_language_points": 4,
                "preserve_source_facts": True,
                "avoid_recent_subjects_and_titles": True,
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

    def _recent_lessons(self, now: datetime, *, limit: int = 6) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT seed_id, result_json
                FROM reading_tutor_sessions
                WHERE substr(created_at, 1, 10) < ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (now.date().isoformat(), limit),
            ).fetchall()
        lessons = []
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError):
                result = {}
            lessons.append(
                {
                    "seed_id": row["seed_id"],
                    "title": str(result.get("title", "")),
                    "topic": str(result.get("topic", "")),
                }
            )
        return lessons

    @staticmethod
    def _select_seed(
        topic: str,
        now: datetime,
        recent_seed_ids: set[str] | None = None,
    ) -> dict:
        matches = (
            list(SEEDS)
            if topic == "mixed"
            else [seed for seed in SEEDS if seed["topic"] == topic]
        )
        if not matches:
            matches = list(SEEDS)
        recent_seed_ids = recent_seed_ids or set()
        fresh = [seed for seed in matches if seed["id"] not in recent_seed_ids]
        candidates = fresh or matches
        key = f"{now.date().isoformat()}|{topic}"
        index = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        return candidates[index % len(candidates)]

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
        "facts, and never claim the adapted passage is a quotation. Avoid repeating recent "
        "subjects and titles. Unless the selected source seed is about computing, do not "
        "default to AI, machine learning, models, agents, or software examples. Input:\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value = None
        for index, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise ValueError("model response did not contain a JSON object")
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value
