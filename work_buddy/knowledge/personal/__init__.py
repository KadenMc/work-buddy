"""SQLite authority for private personal knowledge.

The package deliberately keeps legacy Markdown support behind the explicit
import coordinator.  Ordinary reads and mutations never resolve or inspect a
vault path.
"""

from .provider import (
    PersonalKnowledgeProvider,
    SQLitePersonalKnowledgeProvider,
    get_personal_knowledge_provider,
    set_personal_knowledge_provider,
)
from .importer import (
    PersonalKnowledgeImportCoordinator,
    PersonalKnowledgeImportError,
    inventory_personal_markdown,
)
from .service import PersonalKnowledgeService
from .store import PersonalKnowledgeStore

__all__ = [
    "PersonalKnowledgeProvider",
    "PersonalKnowledgeImportCoordinator",
    "PersonalKnowledgeImportError",
    "PersonalKnowledgeService",
    "PersonalKnowledgeStore",
    "SQLitePersonalKnowledgeProvider",
    "get_personal_knowledge_provider",
    "inventory_personal_markdown",
    "set_personal_knowledge_provider",
]
