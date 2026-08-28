"""Shared, deterministic content matching for research topics."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_IGNORED = {
    "a", "ai", "an", "and", "for", "from", "in", "large", "language", "llm",
    "model", "of", "or", "the", "to", "with",
}


@dataclass(frozen=True)
class TopicContentMatch:
    hits: tuple[str, ...] = ()
    excluded_by: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return bool(self.hits) and not self.excluded_by


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def significant_tokens(value: str) -> frozenset[str]:
    return frozenset(
        stemmed
        for raw in re.findall(r"[a-z0-9]+", value.casefold())
        if len(raw) > 1
        if (stemmed := _stem(raw)) not in _IGNORED
    )


def _significant_sequence(value: str) -> tuple[str, ...]:
    return tuple(
        stemmed
        for raw in re.findall(r"[a-z0-9]+", value.casefold())
        if len(raw) > 1
        if (stemmed := _stem(raw)) not in _IGNORED
    )


def _tokens_are_local(sequence: tuple[str, ...], required: frozenset[str]) -> bool:
    """Require fallback term tokens to occur in one small evidence window."""
    if not required:
        return False
    max_window = max(8, len(required) * 3)
    for start in range(len(sequence)):
        if sequence[start] not in required:
            continue
        present: set[str] = set()
        for token in sequence[start : start + max_window]:
            if token in required:
                present.add(token)
            if present == required:
                return True
    return False


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def term_matches(*, text: str, text_tokens: frozenset[str], term: str) -> bool:
    normalized_term = _normalized(term)
    if not normalized_term:
        return False
    if f" {normalized_term} " in f" {_normalized(text)} ":
        return True
    term_tokens = significant_tokens(term)
    return (
        bool(term_tokens)
        and term_tokens <= text_tokens
        and _tokens_are_local(_significant_sequence(text), term_tokens)
    )


def match_topic_content(
    text: str,
    *,
    include_terms: Iterable[str],
    exclude_terms: Iterable[str] = (),
) -> TopicContentMatch:
    tokens = significant_tokens(text)
    excludes = tuple(
        term for term in exclude_terms
        if term and term_matches(text=text, text_tokens=tokens, term=term)
    )
    if excludes:
        return TopicContentMatch(excluded_by=excludes)
    hits = tuple(
        term for term in include_terms
        if term and term_matches(text=text, text_tokens=tokens, term=term)
    )
    return TopicContentMatch(hits=tuple(dict.fromkeys(hits)))
