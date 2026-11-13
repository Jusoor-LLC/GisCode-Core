"""The AdSpace service: when an ad may be shown, and which provider serves it.

The rules, in one place so the GUI cannot get them wrong:

* **Pro is ad-free.** No provider is consulted and no creative is produced.
* **Off by default.** ``ads.enabled`` must be true.
* **Consent first.** ``ads.consent`` must be true; without it no request would be
  made to a provider at all.
* **Never blocking.** The service is synchronous, local and returns text; it
  never performs a network request in this build, and a caller that uses it for
  a UI panel cannot stall Chat, model selection, project work or attachment
  analysis.
* **Fail open, quietly.** An unreadable setting, an unknown provider or a
  provider that raises yields "no ad", never an exception and never a fabricated
  creative.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from giscode import edition
from giscode.ads.providers import (
    NO_PROVIDER,
    PROVIDER_BY_NAME,
    PROVIDERS,
    AdCreative,
    AdProvider,
    ProviderAvailability,
)


@dataclass(frozen=True)
class ProviderReport:
    """One provider's row in the panel."""

    name: str
    display_name: str
    availability: ProviderAvailability


@dataclass(frozen=True)
class AdSpaceState:
    """Everything the AdSpace panel needs, decided once."""

    edition: str
    ad_free: bool
    enabled: bool
    consent: bool
    provider: str
    available: bool
    reason: str
    source: str = ""
    creative: Optional[AdCreative] = None
    providers: tuple[ProviderReport, ...] = ()

    @property
    def show_ad(self) -> bool:
        """True only when a creative may be rendered."""
        return self.creative is not None and self.available and not self.ad_free

    def describe(self) -> str:
        """A one-line status, suitable for a label or a log."""
        if self.ad_free:
            return "GisCode Pro is ad-free."
        if not self.enabled:
            return "Advertising is off."
        if not self.consent:
            return "Advertising needs your consent."
        if self.show_ad and self.creative is not None:
            return f"Serving a {self.creative.provider} creative."
        return self.reason or "No advertisement is available."


class AdSpace:
    """Reads settings, applies the rules, and reports; never raises."""

    def __init__(
        self,
        settings=None,
        providers: Optional[Iterable[AdProvider]] = None,
    ) -> None:
        self._settings = settings
        configured = tuple(providers) if providers is not None else PROVIDERS
        self._providers = configured
        self._by_name = {provider.name: provider for provider in configured}

    # ------------------------------------------------------------- settings
    def _get(self, key: str, default):
        settings = self._settings
        if settings is None:
            return default
        try:
            return settings.get(key, default)
        except Exception:
            return default

    def _set(self, key: str, value) -> bool:
        settings = self._settings
        if settings is None:
            return False
        try:
            settings.set(key, value)
            return True
        except Exception:
            return False

    # ----------------------------------------------------------------- state
    def provider_reports(self) -> tuple[ProviderReport, ...]:
        reports = []
        for provider in self._providers:
            try:
                availability = provider.availability()
            except Exception:
                availability = ProviderAvailability(
                    available=False, reason="the provider could not be queried"
                )
            reports.append(
                ProviderReport(
                    name=provider.name,
                    display_name=provider.display_name,
                    availability=availability,
                )
            )
        return tuple(reports)

    def state(self) -> AdSpaceState:
        pro = edition.is_pro()
        enabled = bool(self._get("ads.enabled", False))
        consent = bool(self._get("ads.consent", False))
        provider_name = str(self._get("ads.provider", NO_PROVIDER) or NO_PROVIDER).strip().lower()
        reports = self.provider_reports()

        if pro:
            return AdSpaceState(
                edition=edition.PRO,
                ad_free=True,
                enabled=enabled,
                consent=consent,
                provider=provider_name,
                available=False,
                reason="GisCode Pro is ad-free.",
                providers=reports,
            )
        base = dict(
            edition=edition.CORE,
            ad_free=False,
            enabled=enabled,
            consent=consent,
            provider=provider_name,
            providers=reports,
        )
        if not enabled:
            return AdSpaceState(
                available=False, reason="Advertising is turned off in Settings.", **base
            )
        if not consent:
            return AdSpaceState(
                available=False,
                reason="Advertising needs your consent before anything is requested.",
                **base,
            )
        if provider_name in {"", NO_PROVIDER}:
            return AdSpaceState(
                available=False,
                reason=(
                    "No advertising provider is enabled. Unity Ads and AppLovin "
                    "serve mobile platforms only, Microsoft Advertising's Windows "
                    "ad SDK was UWP-only and retired, so none can serve a Windows "
                    "desktop build."
                ),
                **base,
            )
        provider = self._by_name.get(provider_name)
        if provider is None:
            return AdSpaceState(
                available=False,
                reason=f"The '{provider_name}' provider is not part of this build.",
                **base,
            )
        try:
            availability = provider.availability()
        except Exception:
            availability = ProviderAvailability(
                available=False, reason="the provider could not be queried"
            )
        if not availability.available:
            return AdSpaceState(
                available=False,
                reason=availability.reason,
                source=availability.source,
                **base,
            )
        try:
            creative = provider.creative()
        except Exception:
            creative = None
        if creative is None:
            return AdSpaceState(
                available=False,
                reason=f"{provider.display_name} has no creative to show.",
                **base,
            )
        return AdSpaceState(
            available=True,
            reason=f"{provider.display_name} is available.",
            creative=creative,
            **base,
        )

    # ------------------------------------------------------------ user intent
    def set_enabled(self, value: bool) -> bool:
        return self._set("ads.enabled", bool(value))

    def set_consent(self, value: bool) -> bool:
        return self._set("ads.consent", bool(value))

    def set_provider(self, name: str) -> bool:
        return self._set("ads.provider", str(name or NO_PROVIDER).strip().lower())


def provider_names() -> tuple[str, ...]:
    """Names a settings file may legitimately use."""
    return tuple(PROVIDER_BY_NAME) + (NO_PROVIDER,)
