"""Controlled-research interface for the Planer.

The Planer must never have unrestricted internet access. It talks
to the outside world only through this narrow, opt-in interface.

The default implementation is :class:`OfflineResearchProvider`,
which always reports that the real provider is unavailable. This is
the safe default; a future task can plug in a real
``ResearchProvider`` (e.g. an adapter around
:class:`giscode.research.service.ResearchService`) without changing
any Planer code.

The interface is intentionally minimal: ``is_available()``,
``research(query, purpose, project_id) -> ResearchResultRef | None``.

Results are returned as a Planer-local :class:`ResearchResultRef`
record that is clearly marked ``source="research"`` so the future
GUI can distinguish research findings from user-provided
information.
"""
from __future__ import annotations

from typing import Optional, Protocol

from giscode.planer.types import ResearchResultRef, _utc_now_iso


class ResearchProvider(Protocol):
    """Minimal contract the Planer needs from a research backend."""

    def is_available(self) -> bool:
        ...

    def research(
        self,
        query: str,
        purpose: str = "",
        project_id: str = "",
    ) -> Optional[ResearchResultRef]:
        ...


class OfflineResearchProvider:
    """Default research backend: always reports itself as unavailable.

    The Planer uses this default so that the system is safe-by-default
    — no external lookups happen unless a real provider is wired in
    by an explicit configuration step in a future task.
    """

    name = "offline"

    def is_available(self) -> bool:
        return False

    def research(
        self,
        query: str,
        purpose: str = "",
        project_id: str = "",
    ) -> Optional[ResearchResultRef]:
        return None


class AdapterResearchProvider:
    """Adapter around the existing :class:`giscode.research.service.ResearchService`.

    This is provided as a convenience so a future task can plug the
    existing research service in **without** modifying it. Planer
    code only depends on the :class:`ResearchProvider` protocol.

    If the real service is unavailable (which is the current default;
    see ``RealResearchProvider.is_available()`` in
    ``giscode/research/providers.py``), the adapter returns
    ``None`` — exactly like the offline provider.
    """

    def __init__(self, service) -> None:
        self._service = service

    def is_available(self) -> bool:
        try:
            getter = getattr(self._service, "is_available", None)
            if not callable(getter):
                return False
            return bool(getter())
        except Exception:
            return False

    def research(
        self,
        query: str,
        purpose: str = "",
        project_id: str = "",
    ) -> Optional[ResearchResultRef]:
        if not self.is_available():
            return None
        if not query or not query.strip():
            return None
        try:
            from giscode.research.types import (
                ResearchRequest,
                ResearchSourceType,
            )

            request = ResearchRequest(
                request_id=f"planer-{_utc_now_iso()}",
                query=query,
                purpose=purpose or "planer",
                namespace="planer",
                project_id=project_id or None,
                max_results=5,
                timeout=15.0,
                knowledge_store_ingestion=False,
            )
            result = self._service.research(request)
        except Exception:
            return None

        if result is None or not getattr(result, "finding", None):
            return None

        finding = result.finding
        sources: list[dict] = []
        for s in getattr(finding, "sources", []) or []:
            try:
                src_type = s.source_type.value if hasattr(s.source_type, "value") else str(s.source_type)
            except Exception:
                src_type = "unknown"
            sources.append({
                "title": getattr(s, "title", ""),
                "url": getattr(s, "url", ""),
                "domain": getattr(s, "domain", ""),
                "source_type": src_type,
            })
        return ResearchResultRef(
            query=query,
            summary=getattr(finding, "summary", "") or "",
            sources=sources,
            confidence=float(getattr(finding, "confidence", 0.0) or 0.0),
            is_current=bool(getattr(finding, "is_current", False)),
            retrieved_at=_utc_now_iso(),
            purpose=purpose or "",
        )
