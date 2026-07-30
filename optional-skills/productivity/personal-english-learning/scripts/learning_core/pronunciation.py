"""Pronunciation import and deterministic ARPABET display conversion."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database import LearningDatabase


VOWELS = frozenset(
    {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY", "OW", "OY", "UH", "UW"}
)
IPA_VOWELS = {
    "AA": "ɑ",
    "AE": "æ",
    "AO": "ɔ",
    "AW": "aʊ",
    "AY": "aɪ",
    "EH": "ɛ",
    "EY": "eɪ",
    "IH": "ɪ",
    "IY": "iː",
    "OW": "oʊ",
    "OY": "ɔɪ",
    "UH": "ʊ",
    "UW": "uː",
}
IPA_CONSONANTS = {
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ",
    "HH": "h", "JH": "dʒ", "K": "k", "L": "l", "M": "m", "N": "n",
    "NG": "ŋ", "P": "p", "R": "r", "S": "s", "SH": "ʃ", "T": "t",
    "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}
RESPELL_VOWELS = {
    "AA": "ah", "AE": "a", "AO": "aw", "AW": "ow", "AY": "ī",
    "EH": "eh", "EY": "ā", "IH": "ih", "IY": "ē", "OW": "ō",
    "OY": "oy", "UH": "uu", "UW": "oo",
}
RESPELL_CONSONANTS = {
    **{key: value for key, value in IPA_CONSONANTS.items() if len(value) == 1},
    "CH": "ch", "DH": "th", "JH": "j", "NG": "ng", "SH": "sh",
    "TH": "th", "ZH": "zh", "HH": "h", "Y": "y", "R": "r", "G": "g",
}
VALID_ONSETS = frozenset(
    {
        ("P", "R"), ("T", "R"), ("K", "R"), ("B", "R"), ("D", "R"),
        ("G", "R"), ("F", "R"), ("TH", "R"), ("SH", "R"), ("P", "L"),
        ("K", "L"), ("B", "L"), ("G", "L"), ("F", "L"), ("T", "W"),
        ("K", "W"), ("D", "W"), ("G", "W"), ("S", "W"), ("S", "P"),
        ("S", "T"), ("S", "K"), ("S", "M"), ("S", "N"), ("S", "L"),
        ("S", "F"), ("S", "P", "R"), ("S", "T", "R"), ("S", "K", "R"),
        ("S", "P", "L"), ("S", "K", "W"),
    }
)
ENTRY_VARIANT = re.compile(r"^(?P<form>.+?)(?:\((?P<variant>[2-9][0-9]*)\))?$")
PHONE = re.compile(r"^(?P<symbol>[A-Z]+)(?P<stress>[012])?$")


@dataclass(frozen=True)
class ParsedPhone:
    symbol: str
    stress: int | None

    @property
    def is_vowel(self) -> bool:
        return self.symbol in VOWELS


def parse_arpabet(value: str) -> list[ParsedPhone]:
    result = []
    for raw in value.split():
        match = PHONE.fullmatch(raw)
        if not match:
            raise ValueError(f"unsupported ARPABET phone: {raw}")
        phone = ParsedPhone(
            match.group("symbol"),
            int(match.group("stress")) if match.group("stress") is not None else None,
        )
        if phone.is_vowel != (phone.stress is not None):
            raise ValueError(f"invalid ARPABET stress marker: {raw}")
        if not phone.is_vowel and phone.symbol not in IPA_CONSONANTS:
            raise ValueError(f"unsupported ARPABET phone: {raw}")
        result.append(phone)
    if not result or not any(phone.is_vowel for phone in result):
        raise ValueError("ARPABET pronunciation must contain a vowel")
    return result


def _syllable_starts(phones: list[ParsedPhone]) -> list[int]:
    nuclei = [index for index, phone in enumerate(phones) if phone.is_vowel]
    starts = [0]
    for previous, current in zip(nuclei, nuclei[1:]):
        cluster = [phone.symbol for phone in phones[previous + 1 : current]]
        onset_length = 0
        for length in range(1, min(3, len(cluster)) + 1):
            candidate = tuple(cluster[-length:])
            if length == 1 and candidate != ("NG",):
                onset_length = 1
            elif candidate in VALID_ONSETS:
                onset_length = length
        starts.append(current - onset_length)
    return starts


def arpabet_to_ipa(value: str) -> str:
    phones = parse_arpabet(value)
    stress_at = {
        start: phones[nucleus].stress
        for start, nucleus in zip(
            _syllable_starts(phones),
            [index for index, phone in enumerate(phones) if phone.is_vowel],
        )
        if phones[nucleus].stress in {1, 2}
    }
    output = []
    for index, phone in enumerate(phones):
        if index in stress_at:
            output.append("ˈ" if stress_at[index] == 1 else "ˌ")
        if phone.symbol == "AH":
            output.append("ə" if phone.stress == 0 else "ʌ")
        elif phone.symbol == "ER":
            output.append("ɚ" if phone.stress == 0 else "ɝ")
        elif phone.is_vowel:
            output.append(IPA_VOWELS[phone.symbol])
        else:
            output.append(IPA_CONSONANTS[phone.symbol])
    return "".join(output)


def arpabet_to_respelling(value: str) -> str:
    phones = parse_arpabet(value)
    starts = _syllable_starts(phones)
    ends = starts[1:] + [len(phones)]
    syllables = []
    for start, end in zip(starts, ends):
        chunk = phones[start:end]
        rendered = []
        stress = 0
        for phone in chunk:
            if phone.is_vowel:
                stress = max(stress, phone.stress or 0)
                if phone.symbol == "AH":
                    rendered.append("uh")
                elif phone.symbol == "ER":
                    rendered.append("er" if phone.stress == 0 else "ur")
                else:
                    rendered.append(RESPELL_VOWELS[phone.symbol])
            else:
                rendered.append(RESPELL_CONSONANTS[phone.symbol])
        text = "".join(rendered)
        syllables.append(text.upper() if stress == 1 else text)
    return "-".join(syllables)


def stress_pattern(value: str) -> str:
    return "".join(
        str(phone.stress)
        for phone in parse_arpabet(value)
        if phone.is_vowel
    )


class PronunciationService:
    def __init__(self, database: LearningDatabase):
        self.database = database
        self.database.initialize()

    def import_cmudict(
        self,
        path: str | Path,
        *,
        source_version: str = "cmudict-current",
        source_license: str = "CMUdict permissive license",
    ) -> dict:
        source = Path(path).expanduser()
        if not source.is_file():
            raise ValueError(f"CMUdict file not found: {source}")
        with self.database.connect() as connection:
            targets = {
                row["normalized_lemma"]
                for row in connection.execute(
                    "SELECT DISTINCT normalized_lemma FROM word_senses"
                )
            }
            created = updated = skipped = 0
            errors = []
            for line_number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line or line.startswith(";;;"):
                    continue
                fields = line.split(maxsplit=1)
                if len(fields) != 2:
                    skipped += 1
                    continue
                entry, arpabet = fields
                arpabet = arpabet.split(" #", 1)[0].strip()
                match = ENTRY_VARIANT.fullmatch(entry.casefold())
                if not match:
                    skipped += 1
                    continue
                form = match.group("form").replace("_", " ")
                variant = int(match.group("variant") or "1")
                if form not in targets:
                    skipped += 1
                    continue
                try:
                    ipa = arpabet_to_ipa(arpabet)
                    respelling = arpabet_to_respelling(arpabet)
                    pattern = stress_pattern(arpabet)
                except ValueError as exc:
                    if "must contain a vowel" in str(exc):
                        skipped += 1
                        continue
                    errors.append({"line": line_number, "error": str(exc)})
                    continue
                source_entry_id = entry.casefold()
                pronunciation_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"cmudict:{source_entry_id}:{arpabet}")
                )
                existed = connection.execute(
                    "SELECT 1 FROM word_pronunciations WHERE source = ? AND source_entry_id = ?",
                    ("cmudict", source_entry_id),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO word_pronunciations(
                        id, normalized_form, display_form, part_of_speech, dialect,
                        ipa, respelling, arpabet, stress_pattern, variant_rank,
                        source, source_version, source_license, source_entry_id, created_at
                    ) VALUES (?, ?, ?, NULL, 'en-US', ?, ?, ?, ?, ?, 'cmudict', ?, ?, ?, ?)
                    ON CONFLICT(source, source_entry_id) DO UPDATE SET
                        normalized_form = excluded.normalized_form,
                        display_form = excluded.display_form,
                        ipa = excluded.ipa,
                        respelling = excluded.respelling,
                        arpabet = excluded.arpabet,
                        stress_pattern = excluded.stress_pattern,
                        variant_rank = excluded.variant_rank,
                        source_version = excluded.source_version,
                        source_license = excluded.source_license
                    """,
                    (
                        pronunciation_id, form, form, ipa, respelling, arpabet,
                        pattern, variant, source_version, source_license,
                        source_entry_id, datetime.now(timezone.utc).isoformat(),
                    ),
                )
                created += int(existed is None)
                updated += int(existed is not None)
        return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}

    def lookup(
        self,
        form: str,
        *,
        part_of_speech: str | None = None,
        dialect: str = "en-US",
        limit: int = 4,
    ) -> list[dict]:
        normalized = " ".join(form.casefold().strip().split())
        if not normalized:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM word_pronunciations
                WHERE normalized_form = ? AND dialect = ?
                  AND (part_of_speech IS NULL OR part_of_speech = ?)
                ORDER BY CASE WHEN part_of_speech = ? THEN 0 ELSE 1 END,
                         variant_rank, source, source_entry_id
                LIMIT ?
                """,
                (normalized, dialect, part_of_speech, part_of_speech, limit),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def lookup_many(
        self,
        items: list[tuple[str, str | None]],
        *,
        dialect: str = "en-US",
        limit_per_form: int = 4,
    ) -> dict[tuple[str, str | None], list[dict]]:
        """Load pronunciations for several form/POS pairs with one connection."""

        keys = list(
            dict.fromkeys(
                (" ".join(form.casefold().strip().split()), part_of_speech)
                for form, part_of_speech in items
                if form.strip()
            )
        )
        result = {key: [] for key in keys}
        if not keys:
            return result
        forms = list(dict.fromkeys(form for form, _part_of_speech in keys))
        placeholders = ", ".join("?" for _form in forms)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM word_pronunciations
                WHERE normalized_form IN ({placeholders}) AND dialect = ?
                ORDER BY normalized_form, variant_rank, source, source_entry_id
                """,
                (*forms, dialect),
            ).fetchall()
        by_form: dict[str, list] = {form: [] for form in forms}
        for row in rows:
            by_form[row["normalized_form"]].append(row)
        for key in keys:
            form, part_of_speech = key
            matching = [
                row
                for row in by_form[form]
                if row["part_of_speech"] is None
                or row["part_of_speech"] == part_of_speech
            ]
            matching.sort(
                key=lambda row: (
                    0 if row["part_of_speech"] == part_of_speech else 1,
                    row["variant_rank"],
                    row["source"],
                    row["source_entry_id"],
                )
            )
            result[key] = [self._row_dict(row) for row in matching[:limit_per_form]]
        return result

    @staticmethod
    def _row_dict(row) -> dict:
        return {
            "dialect": row["dialect"],
            "ipa": row["ipa"],
            "respelling": row["respelling"],
            "arpabet": row["arpabet"],
            "stress_pattern": row["stress_pattern"],
            "variant_rank": row["variant_rank"],
            "source": row["source"],
        }
