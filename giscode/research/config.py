from dataclasses import dataclass
from typing import Optional


@dataclass
class ResearchConfig:
    enabled: bool = True
    provider: str = "mock"
    max_results: int = 5
    timeout: float = 30.0
    max_query_length: int = 500
    max_response_size: int = 8192
    allowed_domains: Optional[list[str]] = None
    knowledge_store_integration: bool = True
    monitoring_integration: bool = True
