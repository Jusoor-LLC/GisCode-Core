import re
from abc import ABC, abstractmethod
from typing import Optional

from giscode.research.errors import ResearchCancelledError, ResearchProviderUnavailableError, ResearchTimeoutError
from giscode.research.types import ResearchFinding, ResearchProviderType, ResearchRequest, ResearchResult, ResearchSource, ResearchSourceType, ResearchStatus


class ResearchProvider(ABC):
    provider_type: ResearchProviderType = ResearchProviderType.CUSTOM

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def research(self, request: ResearchRequest) -> ResearchResult:
        pass


class MockResearchProvider(ResearchProvider):
    provider_type = ResearchProviderType.MOCK

    def __init__(self, behavior: str = "success"):
        self.behavior = behavior
        self.cancelled = False

    def is_available(self) -> bool:
        return True

    def cancel(self) -> None:
        self.cancelled = True

    def research(self, request: ResearchRequest) -> ResearchResult:
        if self.cancelled:
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.CANCELLED,
                provider=self.provider_type.value,
                error="Research was cancelled",
            )

        if self.behavior == "unavailable":
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.UNAVAILABLE,
                provider=self.provider_type.value,
                error="Mock provider is marked unavailable",
            )

        if self.behavior == "timeout":
            import time
            time.sleep(request.timeout + 1)
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.TIMED_OUT,
                provider=self.provider_type.value,
                error="Mock provider timed out",
            )

        if self.behavior == "no_results":
            return ResearchResult(
                request_id=request.request_id,
                status=ResearchStatus.COMPLETED,
                provider=self.provider_type.value,
                finding=ResearchFinding(
                    summary=f"No results for: {request.query}",
                    sources=[],
                    confidence=0.0,
                    is_current=False,
                ),
            )

        query = request.query.lower()
        if "version" in query or "latest" in query:
            source = ResearchSource(
                title="Mock Package Release Notes",
                url="https://example.com/releases",
                domain="example.com",
                source_type=ResearchSourceType.OFFICIAL_RELEASE_NOTES,
                snippet="Latest version is 1.2.3",
                relevance=0.95,
                authority=0.9,
            )
            finding = ResearchFinding(
                summary=f"Latest version is 1.2.3 for query: {request.query}",
                sources=[source],
                confidence=0.9,
                is_current=True,
            )
        elif "api" in query or "documentation" in query:
            source = ResearchSource(
                title="Mock Official Documentation",
                url="https://docs.example.com/api",
                domain="docs.example.com",
                source_type=ResearchSourceType.OFFICIAL_DOCUMENTATION,
                snippet="The API endpoint accepts GET requests.",
                relevance=0.9,
                authority=0.95,
            )
            finding = ResearchFinding(
                summary=f"API documentation found for: {request.query}",
                sources=[source],
                confidence=0.85,
                is_current=True,
            )
        else:
            source = ResearchSource(
                title="Mock Authoritative Source",
                url="https://example.com/guide",
                domain="example.com",
                source_type=ResearchSourceType.AUTHORITATIVE_SOURCE,
                snippet=f"Information about {request.query}",
                relevance=0.8,
                authority=0.8,
            )
            finding = ResearchFinding(
                summary=f"Found information about {request.query}",
                sources=[source],
                confidence=0.7,
                is_current=False,
            )

        return ResearchResult(
            request_id=request.request_id,
            status=ResearchStatus.COMPLETED,
            provider=self.provider_type.value,
            finding=finding,
        )


class RealResearchProvider(ResearchProvider):
    provider_type = ResearchProviderType.WEB_SEARCH

    def __init__(self, runtime_command: Optional[str] = None):
        self.runtime_command = runtime_command

    def is_available(self) -> bool:
        return False

    def research(self, request: ResearchRequest) -> ResearchResult:
        return ResearchResult(
            request_id=request.request_id,
            status=ResearchStatus.UNAVAILABLE,
            provider=self.provider_type.value,
            error="Real web search provider is not implemented in this foundation",
        )
