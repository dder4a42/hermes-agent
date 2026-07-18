"""Explicit provider registry used by catalog validation and the runner."""

from __future__ import annotations

from .providers.base import SourceProvider
from .models import ProviderOptionSpec


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, SourceProvider] = {}
        self._option_specs: dict[str, ProviderOptionSpec] = {}

    def register(
        self,
        provider_id: str,
        provider: SourceProvider,
        *,
        option_spec: ProviderOptionSpec = ProviderOptionSpec(),
    ) -> None:
        provider_id = provider_id.strip()
        if not provider_id:
            raise ValueError("Provider id cannot be empty")
        if provider_id in self._providers:
            raise ValueError(f"Provider already registered: {provider_id}")
        self._providers[provider_id] = provider
        self._option_specs[provider_id] = option_spec

    def get(self, provider_id: str) -> SourceProvider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise KeyError(f"Unknown source provider: {provider_id}") from None

    def ids(self) -> frozenset[str]:
        return frozenset(self._providers)

    def option_specs(self) -> dict[str, ProviderOptionSpec]:
        return dict(self._option_specs)
