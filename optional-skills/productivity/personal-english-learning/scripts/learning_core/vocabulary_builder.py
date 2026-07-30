"""Build compact, provenance-preserving vocabulary collections from OEWN."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORD_PATTERN = re.compile(r"^[a-z]+(?:[-'][a-z]+)*$")
POS_NAMES = {
    "n": "noun",
    "noun": "noun",
    "v": "verb",
    "verb": "verb",
    "a": "adjective",
    "s": "adjective",
    "adj": "adjective",
    "adjective": "adjective",
    "r": "adverb",
    "adv": "adverb",
    "adverb": "adverb",
}
OEWN_POS_CODES = {
    "noun": {"n"},
    "verb": {"v"},
    "adjective": {"a", "s"},
    "adverb": {"r"},
}


@dataclass(frozen=True)
class RankedLemma:
    lemma: str
    rank: int
    part_of_speech: str | None = None


@dataclass(frozen=True)
class CollectionSpec:
    id: str
    title: str
    kind: str
    source: str
    version: str
    license: str

    def __post_init__(self) -> None:
        if not all(
            value and value.strip()
            for value in (
                self.id,
                self.title,
                self.source,
                self.version,
                self.license,
            )
        ):
            raise ValueError("collection specification fields are required")
        if self.kind not in {"general", "academic", "custom"}:
            raise ValueError("collection kind must be general, academic, or custom")


class VocabularyBuilder:
    """Select learner-sized OEWN senses using an external ranked lemma list."""

    def __init__(
        self,
        wordnet_zip: str | Path,
        *,
        sense_index_zip: str | Path | None = None,
        wordnet_version: str = "2025",
    ):
        self.wordnet_zip = Path(wordnet_zip).expanduser()
        self.sense_index_zip = Path(sense_index_zip or wordnet_zip).expanduser()
        self.wordnet_version = wordnet_version.strip()
        if not self.wordnet_version:
            raise ValueError("wordnet_version is required")
        if not self.wordnet_zip.is_file():
            raise ValueError(
                f"Open English WordNet archive not found: {self.wordnet_zip}"
            )
        if not zipfile.is_zipfile(self.wordnet_zip):
            raise ValueError("wordnet_zip must be a valid ZIP archive")
        if not self.sense_index_zip.is_file() or not zipfile.is_zipfile(
            self.sense_index_zip
        ):
            raise ValueError("sense_index_zip must be a valid ZIP archive")
        self._sense_ranks = self._load_sense_ranks()

    def build(
        self,
        ranked_lemmas: Iterable[RankedLemma],
        output: str | Path,
        collection: CollectionSpec,
        *,
        frequency_ranks: dict[str, int] | None = None,
        lemma_limit: int = 5000,
        max_senses_per_lemma: int = 2,
        force: bool = False,
        ranked_source_path: str | Path | None = None,
        frequency_source_path: str | Path | None = None,
    ) -> dict[str, Any]:
        if lemma_limit <= 0 or max_senses_per_lemma <= 0:
            raise ValueError("lemma_limit and max_senses_per_lemma must be positive")
        output_path = Path(output).expanduser()
        manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
        if not force and (output_path.exists() or manifest_path.exists()):
            raise ValueError("output or manifest already exists; pass force to replace")

        ranked = self._deduplicate_ranked_lemmas(ranked_lemmas)
        selected_entries = self._load_entries(ranked)
        selected_synset_ids = {
            sense["synset"]
            for entry in selected_entries.values()
            for senses in entry.values()
            for sense in senses
        }
        synsets = self._load_synsets(selected_synset_ids)
        frequency_ranks = frequency_ranks or {}
        rows: list[dict[str, Any]] = []
        selected_lemma_count = 0
        unmatched: list[str] = []

        for item in ranked:
            if selected_lemma_count >= lemma_limit:
                break
            by_pos = selected_entries.get(item.lemma)
            if not by_pos:
                unmatched.append(item.lemma)
                continue
            senses: list[tuple[str, dict[str, str]]] = []
            allowed_codes = (
                OEWN_POS_CODES[item.part_of_speech]
                if item.part_of_speech is not None
                else None
            )
            pos_codes = [
                code
                for code in ("n", "v", "a", "s", "r")
                if allowed_codes is None or code in allowed_codes
            ]
            max_depth = max(
                (len(by_pos.get(code, [])) for code in pos_codes),
                default=0,
            )
            for sense_index in range(max_depth):
                for pos_code in pos_codes:
                    pos_senses = by_pos.get(pos_code, [])
                    if sense_index < len(pos_senses):
                        senses.append((pos_code, pos_senses[sense_index]))
            emitted = 0
            for pos_code, sense in senses:
                synset = synsets.get(sense["synset"])
                if not synset or not synset.get("definition"):
                    continue
                rows.append(
                    {
                        "lemma": item.lemma,
                        "part_of_speech": POS_NAMES[pos_code],
                        "definition_en": str(synset["definition"][0]).strip(),
                        "definition_zh": None,
                        "frequency_rank": frequency_ranks.get(item.lemma),
                        "source": f"oewn-{self.wordnet_version}",
                        "source_sense_id": sense["id"],
                        "collection": {
                            "id": collection.id,
                            "title": collection.title,
                            "kind": collection.kind,
                            "source": collection.source,
                            "version": collection.version,
                            "license": collection.license,
                            "rank": item.rank,
                            "sense_rank": emitted + 1,
                            "source_lemma": item.lemma,
                        },
                    }
                )
                emitted += 1
                if emitted >= max_senses_per_lemma:
                    break
            if emitted:
                selected_lemma_count += 1
            else:
                unmatched.append(item.lemma)

        if not rows:
            raise ValueError(
                "no ranked lemmas matched usable Open English WordNet senses"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        )
        output_path.write_text(serialized, encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "lexical_source": {
                "name": "Open English WordNet",
                "version": self.wordnet_version,
                "url": "https://en-word.net/",
                "license": "CC-BY-4.0",
            },
            "collection": {
                "id": collection.id,
                "title": collection.title,
                "kind": collection.kind,
                "source": collection.source,
                "version": collection.version,
                "license": collection.license,
            },
            "inputs": {
                "wordnet": self._file_identity(self.wordnet_zip),
                "sense_index": self._file_identity(self.sense_index_zip),
                "ranked_lemmas": self._ranked_identity(
                    ranked_source_path, ranked
                ),
                "frequency_ranks": self._frequency_identity(
                    frequency_source_path, frequency_ranks
                ),
            },
            "parameters": {
                "lemma_limit": lemma_limit,
                "max_senses_per_lemma": max_senses_per_lemma,
                "wordnet_version": self.wordnet_version,
            },
            "result": {
                "ranked_lemmas": len(ranked),
                "selected_lemmas": selected_lemma_count,
                "sense_entries": len(rows),
                "unmatched_lemmas": unmatched,
                "output_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "output": str(output_path),
            "manifest": str(manifest_path),
            **manifest["result"],
        }

    @staticmethod
    def _deduplicate_ranked_lemmas(
        ranked_lemmas: Iterable[RankedLemma],
    ) -> list[RankedLemma]:
        result: list[RankedLemma] = []
        seen: set[tuple[str, str | None]] = set()
        for item in sorted(ranked_lemmas, key=lambda value: (value.rank, value.lemma)):
            lemma = normalize_source_lemma(item.lemma)
            if not lemma or not WORD_PATTERN.fullmatch(lemma):
                continue
            pos = normalize_part_of_speech(item.part_of_speech)
            identity = (lemma, pos)
            if identity in seen:
                continue
            if item.rank <= 0:
                raise ValueError("ranked lemma ranks must be positive")
            seen.add(identity)
            result.append(RankedLemma(lemma, item.rank, pos))
        return result

    def _load_entries(
        self, ranked: Iterable[RankedLemma]
    ) -> dict[str, dict[str, list[dict[str, str]]]]:
        targets = {item.lemma for item in ranked}
        result: dict[str, dict[str, list[dict[str, str]]]] = {}
        with zipfile.ZipFile(self.wordnet_zip) as archive:
            entry_names = sorted(
                name
                for name in archive.namelist()
                if Path(name).name.startswith("entries-") and name.endswith(".json")
            )
            if not entry_names:
                raise ValueError("wordnet_zip has no entries-*.json files")
            for name in entry_names:
                payload = json.loads(archive.read(name))
                for source_lemma, pos_entries in payload.items():
                    lemma = normalize_source_lemma(source_lemma)
                    if lemma not in targets:
                        continue
                    target = result.setdefault(lemma, {})
                    for pos_code, entry in pos_entries.items():
                        target.setdefault(pos_code, []).extend(entry.get("sense", []))
        for pos_entries in result.values():
            for senses in pos_entries.values():
                senses.sort(key=self._sense_sort_key)
        return result

    def _load_sense_ranks(self) -> dict[str, tuple[int, int]]:
        with zipfile.ZipFile(self.sense_index_zip) as archive:
            matches = [
                name for name in archive.namelist() if Path(name).name == "index.sense"
            ]
            if len(matches) != 1:
                raise ValueError("sense_index_zip must contain exactly one index.sense")
            result: dict[str, tuple[int, int]] = {}
            for raw_line in archive.read(matches[0]).decode("utf-8").splitlines():
                fields = raw_line.split()
                if len(fields) != 4:
                    continue
                sense_id, _, sense_number, tag_count = fields
                result[sense_id] = (int(sense_number), int(tag_count))
        if not result:
            raise ValueError("index.sense contains no usable sense rankings")
        return result

    def _sense_sort_key(self, sense: dict[str, str]) -> tuple[int, int, str]:
        sense_number, tag_count = self._sense_ranks.get(
            sense.get("id", ""),
            (1_000_000, 0),
        )
        return (-tag_count, sense_number, sense.get("id", ""))

    def _load_synsets(self, target_ids: set[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        with zipfile.ZipFile(self.wordnet_zip) as archive:
            synset_names = sorted(
                name
                for name in archive.namelist()
                if name.endswith(".json")
                and not Path(name).name.startswith("entries-")
                and Path(name).name != "frames.json"
            )
            for name in synset_names:
                payload = json.loads(archive.read(name))
                for synset_id in target_ids.intersection(payload):
                    result[synset_id] = payload[synset_id]
        return result

    @staticmethod
    def _file_identity(path: Path) -> dict[str, Any]:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        }

    @classmethod
    def _ranked_identity(
        cls,
        path: str | Path | None,
        items: Iterable[RankedLemma],
    ) -> dict[str, Any]:
        if path is not None:
            return cls._file_identity(Path(path).expanduser())
        canonical = "".join(
            f"{item.rank}\t{item.lemma}\t{item.part_of_speech or ''}\n"
            for item in items
        )
        return {
            "kind": "generated",
            "count": canonical.count("\n"),
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    @classmethod
    def _frequency_identity(
        cls,
        path: str | Path | None,
        ranks: dict[str, int],
    ) -> dict[str, Any] | None:
        if path is not None:
            return cls._file_identity(Path(path).expanduser())
        if not ranks:
            return None
        canonical = "".join(
            f"{rank}\t{lemma}\n"
            for lemma, rank in sorted(
                ranks.items(), key=lambda item: (item[1], item[0])
            )
        )
        return {
            "kind": "generated",
            "count": len(ranks),
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }


def normalize_source_lemma(value: str) -> str:
    return " ".join(str(value).casefold().strip().split()).replace("_", " ")


def normalize_part_of_speech(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = POS_NAMES.get(str(value).casefold().strip())
    if normalized is None:
        raise ValueError(f"unsupported part of speech: {value}")
    return normalized


def load_ranked_lemmas(path: str | Path) -> list[RankedLemma]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"ranked lemma list not found: {source}")
    if source.suffix.casefold() == ".jsonl":
        return _load_jsonl_ranked_lemmas(source)
    if source.suffix.casefold() == ".xlsx":
        raise ValueError("XLSX is not supported; export the worksheet as UTF-8 CSV")
    return _load_delimited_ranked_lemmas(source)


def load_wordfreq_lemmas(limit: int) -> list[RankedLemma]:
    if limit <= 0:
        raise ValueError("frequency limit must be positive")
    try:
        from wordfreq import top_n_list
    except ImportError as exc:
        raise ValueError(
            "wordfreq is not installed; provide --frequency-list or install wordfreq"
        ) from exc
    return [
        RankedLemma(str(lemma), rank)
        for rank, lemma in enumerate(top_n_list("en", limit), start=1)
    ]


def frequency_rank_map(items: Iterable[RankedLemma]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in sorted(items, key=lambda value: value.rank):
        lemma = normalize_source_lemma(item.lemma)
        if lemma and lemma not in result:
            result[lemma] = item.rank
    return result


def _load_jsonl_ranked_lemmas(path: Path) -> list[RankedLemma]:
    result: list[RankedLemma] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("line must contain an object")
                lemma = value.get("lemma", value.get("word", ""))
                rank = value.get(
                    "rank",
                    value.get(
                        "academic_rank", value.get("frequency_rank", line_number)
                    ),
                )
                pos = value.get("part_of_speech", value.get("pos"))
                result.append(RankedLemma(str(lemma), int(rank), pos))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid ranked JSONL line {line_number}: {exc}"
                ) from exc
    return result


def _load_delimited_ranked_lemmas(path: Path) -> list[RankedLemma]:
    lines = [
        line
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return []
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        return [RankedLemma(line.strip(), rank) for rank, line in enumerate(lines, 1)]
    rows = list(csv.reader(lines, dialect))
    header = [cell.casefold().strip() for cell in rows[0]]
    lemma_names = ("lemma", "word", "headword")
    has_header = any(name in header for name in lemma_names)
    if not has_header:
        result = []
        for fallback_rank, row in enumerate(rows, 1):
            if not row:
                continue
            if row[0].strip().isdigit() and len(row) >= 2:
                rank, lemma = int(row[0]), row[1]
                pos = row[2] if len(row) >= 3 else None
            else:
                lemma, rank = row[0], fallback_rank
                pos = row[1] if len(row) >= 2 else None
            result.append(RankedLemma(lemma, rank, pos))
        return result

    columns = {name: index for index, name in enumerate(header)}
    lemma_index = next(columns[name] for name in lemma_names if name in columns)
    rank_index = next(
        (
            columns[name]
            for name in ("rank", "academic_rank", "frequency_rank")
            if name in columns
        ),
        None,
    )
    pos_index = next(
        (columns[name] for name in ("part_of_speech", "pos") if name in columns),
        None,
    )
    result = []
    for fallback_rank, row in enumerate(rows[1:], 1):
        if len(row) <= lemma_index or not row[lemma_index].strip():
            continue
        rank = (
            int(row[rank_index])
            if rank_index is not None and row[rank_index]
            else fallback_rank
        )
        pos = row[pos_index] if pos_index is not None and len(row) > pos_index else None
        result.append(RankedLemma(row[lemma_index], rank, pos))
    return result
