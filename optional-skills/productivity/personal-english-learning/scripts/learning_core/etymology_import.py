"""Streaming, source-preserving etymology import from Wiktextract JSONL."""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path
from collections.abc import Iterator
from typing import Any, TextIO
from urllib.parse import quote

from .lexical_analysis import LexicalAnalysisService, normalize_form


KAIKKI_SOURCE = "kaikki-enwiktionary"
KAIKKI_LICENSE = "CC-BY-SA-4.0 / GFDL"
MAX_TEMPLATES_PER_ENTRY = 64
MAX_TEMPLATE_EXPANSION = 1000
POS_MAP = {
    "noun": "noun",
    "verb": "verb",
    "adj": "adjective",
    "adjective": "adjective",
    "adv": "adverb",
    "adverb": "adverb",
}


def _open_jsonl(path: Path) -> TextIO:
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _read_lines(path: Path) -> Iterator[str]:
    try:
        with _open_jsonl(path) as handle:
            yield from handle
    except (EOFError, gzip.BadGzipFile) as exc:
        raise ValueError(
            f"Kaikki gzip file is incomplete or invalid: {path}"
        ) from exc


def _template_expansions(templates: Any) -> list[str]:
    if not isinstance(templates, list):
        return []
    expansions = []
    for template in templates:
        if not isinstance(template, dict):
            continue
        expansion = template.get("expansion")
        if (
            isinstance(expansion, str)
            and expansion.strip()
            and len(expansion) <= MAX_TEMPLATE_EXPANSION
        ):
            expansions.append(" ".join(expansion.split()))
    return list(dict.fromkeys(expansions))


def _clean_templates(templates: Any) -> list[dict[str, Any]]:
    if not isinstance(templates, list):
        return []
    cleaned = []
    for template in templates[:MAX_TEMPLATES_PER_ENTRY]:
        if not isinstance(template, dict):
            continue
        item: dict[str, Any] = {}
        name = template.get("name")
        if isinstance(name, str) and name:
            item["name"] = name
        args = template.get("args")
        if isinstance(args, dict):
            item["args"] = {
                str(key): value
                for key, value in args.items()
                if isinstance(value, (int, float, bool))
                or (isinstance(value, str) and len(value) <= MAX_TEMPLATE_EXPANSION)
            }
        expansion = template.get("expansion")
        if (
            isinstance(expansion, str)
            and expansion.strip()
            and len(expansion) <= MAX_TEMPLATE_EXPANSION
        ):
            item["expansion"] = " ".join(expansion.split())
        if item:
            cleaned.append(item)
    return cleaned


def _source_url(word: str) -> str:
    page = quote(word.replace(" ", "_"), safe="-_.~()")
    return f"https://en.wiktionary.org/wiki/{page}#English"


class KaikkiEtymologyImporter:
    """Import only English entries that intersect the installed vocabulary."""

    def __init__(self, lexical_service: LexicalAnalysisService):
        self.lexical_service = lexical_service
        self.database = lexical_service.database

    def import_file(
        self,
        path: str | Path,
        *,
        source_version: str,
        source_license: str = KAIKKI_LICENSE,
    ) -> dict:
        source_path = Path(path).expanduser()
        if not source_path.is_file():
            raise ValueError(f"Kaikki JSONL file not found: {source_path}")
        source_version = source_version.strip()
        source_license = source_license.strip()
        if not source_version:
            raise ValueError("source_version is required")
        if not source_license:
            raise ValueError("source_license is required")

        with self.database.connect() as connection:
            installed = {
                (row["normalized_lemma"], row["part_of_speech"])
                for row in connection.execute(
                    "SELECT DISTINCT normalized_lemma, part_of_speech FROM word_senses"
                )
            }
        installed_forms = {form for form, _pos in installed}
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        lines = english_entries = matched_entries = skipped_without_etymology = 0
        error_count = 0
        errors: list[dict[str, Any]] = []

        for line_number, line in enumerate(_read_lines(source_path), start=1):
            lines += 1
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("line must contain one JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                error_count += 1
                if len(errors) < 100:
                    errors.append({"line": line_number, "error": str(exc)})
                continue
            if raw.get("lang_code") != "en":
                continue
            english_entries += 1
            word = raw.get("word")
            if not isinstance(word, str):
                continue
            form = normalize_form(word)
            if form not in installed_forms:
                continue
            part_of_speech = POS_MAP.get(str(raw.get("pos", "")).casefold())
            if not part_of_speech or (form, part_of_speech) not in installed:
                continue
            text = raw.get("etymology_text")
            text = " ".join(text.split()) if isinstance(text, str) else ""
            expansions = _template_expansions(raw.get("etymology_templates"))
            if not text and expansions:
                text = " ".join(expansions)
            if not text:
                skipped_without_etymology += 1
                continue
            matched_entries += 1
            grouped[(form, part_of_speech)].append(
                {
                    "etymology_number": raw.get("etymology_number"),
                    "text": text,
                    "templates": _clean_templates(raw.get("etymology_templates")),
                    "source_url": _source_url(word),
                }
            )

        payloads = []
        ambiguous = 0
        for (form, part_of_speech), records in sorted(grouped.items()):
            unique = []
            seen = set()
            for record in records:
                identity = (record["etymology_number"], record["text"])
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append(record)
            is_ambiguous = len({item["text"] for item in unique}) > 1
            ambiguous += int(is_ambiguous)
            content: dict[str, Any] = {
                "source_url": unique[0]["source_url"],
                "etymology_number": unique[0]["etymology_number"],
            }
            if is_ambiguous:
                content["alternatives"] = unique
            else:
                content.update(
                    etymology_text=unique[0]["text"],
                    templates=unique[0]["templates"],
                )
            payloads.append(
                {
                    "form": form,
                    "part_of_speech": part_of_speech,
                    "analysis_type": "historical_etymology",
                    "status": "ambiguous" if is_ambiguous else "available",
                    "source_level": "authoritative",
                    "content": content,
                    "confidence": 0.95,
                    "source": KAIKKI_SOURCE,
                    "source_version": source_version,
                    "source_license": source_license,
                    "source_entry_id": f"{form}|{part_of_speech}",
                }
            )

        written = self.lexical_service.upsert_analyses(payloads)
        return {
            **written,
            "lines_read": lines,
            "english_entries": english_entries,
            "matched_entries": matched_entries,
            "imported_forms": len(payloads),
            "ambiguous_forms": ambiguous,
            "skipped_without_etymology": skipped_without_etymology,
            "error_count": error_count,
            "errors": errors,
            "source": KAIKKI_SOURCE,
            "source_version": source_version,
            "source_license": source_license,
        }
