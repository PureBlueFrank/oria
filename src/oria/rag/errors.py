"""Safe T05 knowledge-boundary errors."""


class KnowledgeError(RuntimeError):
    """A safe catalog, object, index, or retrieval failure."""


class ObjectStoreError(KnowledgeError):
    """Local object operation failed closed."""


class CatalogError(KnowledgeError):
    """Platform knowledge catalog operation failed closed."""


class IndexError(KnowledgeError):
    """Vector projection operation failed closed."""


class RuleSnapshotError(KnowledgeError):
    """Rule snapshot is unavailable, unresolved, stale, or modified."""
