"""SQLite-backed Research Library primitives."""

from .database import connect_library, initialize_library
from .models import (
    ResearchItemDraft,
    SourceEvidence,
    SourceRunCounts,
    TopicMatch,
    UpsertResult,
)
from .repository import LibraryRepository

__all__ = [
    "LibraryRepository",
    "ResearchItemDraft",
    "SourceEvidence",
    "SourceRunCounts",
    "TopicMatch",
    "UpsertResult",
    "connect_library",
    "initialize_library",
]
