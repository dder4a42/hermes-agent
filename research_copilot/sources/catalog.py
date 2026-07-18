"""Strict YAML Source Catalog loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping

import yaml

from .models import ProviderOptionSpec, RetryPolicy, SourceBudget, SourceDefinition

SCHEMA_VERSION = 1
_ROOT_FIELDS = {"schema_version", "sources"}
_SOURCE_FIELDS = {
    "id", "provider", "display_name", "type", "enabled", "tier",
    "topics", "budget", "retry", "options",
}
_BUDGET_FIELDS = {"max_requests", "max_items", "max_items_per_topic"}
_RETRY_FIELDS = {"max_attempts", "backoff_seconds"}


class SourceCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class SourceCatalog:
    schema_version: int
    sources: tuple[SourceDefinition, ...]

    def by_id(self, source_id: str) -> SourceDefinition:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceCatalogError(f"{location} must be a mapping")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], location: str) -> None:
    if unknown := sorted(set(data) - allowed):
        raise SourceCatalogError(f"Unknown fields at {location}: {', '.join(unknown)}")


def load_source_catalog(
    path: str | Path,
    *,
    provider_ids: Collection[str],
    topic_ids: Collection[str],
    option_specs: Mapping[str, ProviderOptionSpec] | None = None,
) -> SourceCatalog:
    catalog_path = Path(path)
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceCatalogError(f"Cannot read Source Catalog {catalog_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SourceCatalogError(f"Invalid Source Catalog YAML: {exc}") from exc

    root = _mapping(raw, "catalog")
    _reject_unknown(root, _ROOT_FIELDS, "catalog")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise SourceCatalogError(
            f"Unsupported Source Catalog schema {root.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )
    source_rows = root.get("sources")
    if not isinstance(source_rows, list):
        raise SourceCatalogError("catalog.sources must be a list")

    known_providers = set(provider_ids)
    known_topics = set(topic_ids)
    seen: set[str] = set()
    sources: list[SourceDefinition] = []
    for index, value in enumerate(source_rows):
        location = f"sources[{index}]"
        data = _mapping(value, location)
        _reject_unknown(data, _SOURCE_FIELDS, location)
        source_id = str(data.get("id") or "").strip()
        if source_id in seen:
            raise SourceCatalogError(f"Duplicate source id: {source_id}")
        seen.add(source_id)
        provider = str(data.get("provider") or "").strip()
        if provider not in known_providers:
            raise SourceCatalogError(f"Unknown provider for {source_id or location}: {provider or '<empty>'}")

        topics_value = data.get("topics", ["*"])
        if not isinstance(topics_value, list) or not all(isinstance(t, str) for t in topics_value):
            raise SourceCatalogError(f"{location}.topics must be a list of strings")
        topics = tuple(topics_value)
        unknown_topics = sorted(set(topics) - known_topics - {"*"})
        if unknown_topics:
            raise SourceCatalogError(f"Unknown topics for {source_id}: {', '.join(unknown_topics)}")

        budget_data = _mapping(data.get("budget", {}), f"{location}.budget")
        retry_data = _mapping(data.get("retry", {}), f"{location}.retry")
        options = _mapping(data.get("options", {}), f"{location}.options")
        _reject_unknown(budget_data, _BUDGET_FIELDS, f"{location}.budget")
        _reject_unknown(retry_data, _RETRY_FIELDS, f"{location}.retry")
        if option_specs is not None:
            spec = option_specs.get(provider)
            if spec is None:
                raise SourceCatalogError(f"Missing option schema for provider: {provider}")
            _reject_unknown(options, set(spec.allowed), f"{location}.options")
            if missing := sorted(spec.required - set(options)):
                raise SourceCatalogError(
                    f"Missing required options for {source_id}: {', '.join(missing)}"
                )
        try:
            source = SourceDefinition(
                id=source_id,
                provider=provider,
                display_name=str(data.get("display_name") or "").strip(),
                source_type=str(data.get("type") or "").strip(),
                enabled=data.get("enabled", True),
                tier=float(data.get("tier", 0.5)),
                topics=topics,
                budget=SourceBudget(**budget_data),
                retry=RetryPolicy(**retry_data),
                options=options,
            )
        except (TypeError, ValueError) as exc:
            raise SourceCatalogError(f"Invalid {location}: {exc}") from exc
        if not isinstance(source.enabled, bool):
            raise SourceCatalogError(f"{location}.enabled must be a boolean")
        sources.append(source)
    return SourceCatalog(schema_version=SCHEMA_VERSION, sources=tuple(sources))
