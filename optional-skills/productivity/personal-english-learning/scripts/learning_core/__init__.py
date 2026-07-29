"""Local learning core for the personal English learning skill."""

from .database import LearningDatabase
from .service import LearningService, VocabularyEntry

__all__ = ["LearningDatabase", "LearningService", "VocabularyEntry"]
