class KnowledgeStoreError(Exception):
    pass


class NamespaceError(KnowledgeStoreError):
    pass


class ValidationError(KnowledgeStoreError):
    pass


class StorageError(KnowledgeStoreError):
    pass


class RetrievalError(KnowledgeStoreError):
    pass


class DuplicateRecordError(KnowledgeStoreError):
    pass


class RecordNotFoundError(KnowledgeStoreError):
    pass


class PathSafetyError(KnowledgeStoreError):
    pass


class UnsupportedFormatError(KnowledgeStoreError):
    pass
