from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class ResearchStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    ENABLED = "enabled"


class ResearchSourceType(Enum):
    OFFICIAL_DOCUMENTATION = "official_documentation"
    OFFICIAL_RELEASE_NOTES = "official_release_notes"
    OFFICIAL_REPOSITORY = "official_repository"
    PACKAGE_REGISTRY = "package_registry"
    AUTHORITATIVE_SOURCE = "authoritative_source"
    REPUTABLE_TECHNICAL = "reputable_technical"
    UNKNOWN = "unknown"


class ResearchProviderType(Enum):
    MOCK = "mock"
    WEB_SEARCH = "web_search"
    CUSTOM = "custom"


@dataclass
class ResearchSource:
    title: str
    url: str
    domain: str
    source_type: ResearchSourceType = ResearchSourceType.UNKNOWN
    published_at: Optional[str] = None
    retrieved_at: str = field(default_factory=lambda: datetime.now().isoformat())
    relevance: float = 0.0
    authority: float = 0.0
    snippet: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ResearchFinding:
    summary: str
    sources: list[ResearchSource] = field(default_factory=list)
    confidence: float = 0.0
    is_current: bool = False
    retrieval_timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)


@dataclass
class ResearchRequest:
    request_id: str
    query: str
    purpose: str = ""
    namespace: str = "default"
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    max_results: int = 5
    timeout: float = 30.0
    allowed_domains: Optional[list[str]] = None
    knowledge_store_ingestion: bool = True
    provider: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id is required")
        if not self.query or not self.query.strip():
            raise ValueError("query is required")
        if self.max_results < 1:
            raise ValueError("max_results must be >= 1")
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")


@dataclass
class ResearchResult:
    request_id: str
    status: ResearchStatus
    finding: Optional[ResearchFinding] = None
    error: Optional[str] = None
    provider: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)
