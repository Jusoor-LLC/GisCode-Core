from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ModelStatus(Enum):
    UNKNOWN = "unknown"
    REGISTERED = "registered"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    UNLOADING = "unloading"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class ModelMetadata:
    name: str
    family: str
    path: str
    context_length: int = 4096
    gpu_layers: int = 0
    size_bytes: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    status: ModelStatus = ModelStatus.UNKNOWN
    error_message: Optional[str] = None
    #: Where this record came from: ``catalog`` (declared) or ``discovered``
    #: (a file found on disk). Empty means "not recorded", which capability
    #: classification treats as discovered.
    source: str = ""
    #: Header digest of the model file (see giscode.models.capability): a
    #: content-derived index key that follows a renamed file, so a capability
    #: record can be found again. It is not proof of the weights - the store
    #: re-hashes the whole file before trusting a record.
    identity: str = ""
    #: ``{capability: {level, source, detail, verified}}`` - how strongly each
    #: capability is known. ``capabilities`` stays the plain list for existing
    #: callers; this adds the provenance the list cannot express.
    capability_evidence: dict = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        return capability.lower() in [c.lower() for c in self.capabilities]

    def evidence_level(self, capability: str) -> str:
        """Evidence level for one capability, or ``"unrecorded"``."""
        record = (self.capability_evidence or {}).get(str(capability).lower())
        if isinstance(record, dict):
            return str(record.get("level") or "unrecorded")
        return "unrecorded"


class ModelRegistry:
    def __init__(self):
        self._models: dict[str, ModelMetadata] = {}

    def register(self, metadata: ModelMetadata) -> None:
        self._models[metadata.name] = metadata

    def get(self, name: str) -> Optional[ModelMetadata]:
        return self._models.get(name)

    def resolve_canonical_name(self, identity: str) -> Optional[ModelMetadata]:
        """Resolve a runtime model identity to its registered metadata.

        The identity may be a canonical registered name or a file-derived one
        such as a GGUF stem that carries a quantization suffix
        (e.g. ``Qwen3-VL-4B-Instruct-Q4_K_M`` for the registered
        ``Qwen3-VL-4B-Instruct``). Exact matches always win; otherwise the
        registered name and the identity are matched against each other, which
        is the same rule discovery already uses to name GGUF files after their
        catalog entry.
        """
        if not identity:
            return None
        exact = self._models.get(identity)
        if exact is not None:
            return exact
        identity_lower = identity.lower()
        for metadata in self._models.values():
            name = metadata.name
            if not name:
                continue
            name_lower = name.lower()
            if name_lower in identity_lower or identity_lower in name_lower:
                return metadata
        return None

    def list_all(self) -> list[ModelMetadata]:
        return list(self._models.values())

    def remove(self, name: str) -> None:
        self._models.pop(name, None)
