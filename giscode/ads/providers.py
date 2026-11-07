"""Advertising providers behind one small adapter interface.

The GisCode AdSpace panel talks to :class:`giscode.ads.service.AdSpace`, never to
a provider directly, so adding or replacing a provider does not touch the GUI.

## Why the two requested providers are not integrated

Unity Ads and AppLovin were the two providers the owner asked for. Both were
checked against their current official documentation (October 2026):

* **Unity Ads** - "Unity Ads is an ad network for iOS (iTunes) and Android
  (Google Play)". The same FAQ lists the platforms it does not support, and the
  Ads Mediation package manifest states ``Supported platforms: Android, iOS``.
  There is no Windows SDK, no .NET binding and no official web/JavaScript SDK;
  the docs explicitly exclude WebGL/WebPlayer.
  <https://docs.unity.com/en-us/grow/ads/references/faq>
  <https://docs.unity.com/en-us/engine/6000.7/manual/packages-list/packages-all/pack-safe/com-unity-services-levelplay>
* **AppLovin (MAX)** - documented SDK targets are Android (including Amazon
  Fire OS), iOS and the engine wrappers that build to those (Unity, Adobe AIR,
  Unreal, Cocos2d-x, React Native, Flutter, .NET/MAUI, Godot, Cordova). The
  publisher policies require integration "as described in the AppLovin
  technical integration documentation" and accurate traffic-source disclosure;
  the EULA limits the SDK licence to use "solely for purposes of your access to
  and use of the Platform and the Services". A Windows EXE or Python GUI has no
  documented integration and would misrepresent its inventory.
  <https://legal.applovin.com/policies-publishers/>
  <https://legal.applovin.com/eula/>

Neither provider can therefore serve GisCode Core on Windows. Their adapters are
present and honest - they report *unavailable* with the reason and the source -
so the panel, the settings and the decision are all in place, and a provider
that does support desktop can be added without redesigning anything.

Nothing here performs a network request, renders remote content or opens a
webview. A future adapter that does must gate every request on the user's
consent and on ``ads.enabled``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderAvailability:
    """Whether a provider can serve an ad in *this* application, and why."""

    available: bool
    reason: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason, "source": self.source}


@dataclass(frozen=True)
class AdCreative:
    """One creative, already rendered as text.

    Text only, on purpose: no remote HTML, scriptable content or executable
    payload can enter the application through this type.
    """

    provider: str
    headline: str
    body: str
    #: Always shown. A house promotion must never look like a paid third-party ad.
    disclosure: str = ""


@runtime_checkable
class AdProvider(Protocol):
    """What the AdSpace service needs from a provider."""

    name: str
    display_name: str

    def availability(self) -> ProviderAvailability:
        ...

    def creative(self) -> Optional[AdCreative]:
        ...


class _UnavailableProvider:
    """Base for a provider that cannot serve this application."""

    name = ""
    display_name = ""
    _reason = ""
    _source = ""

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(available=False, reason=self._reason, source=self._source)

    def creative(self) -> Optional[AdCreative]:
        return None


class UnityAdsProvider(_UnavailableProvider):
    name = "unity_ads"
    display_name = "Unity Ads"
    _reason = (
        "Unity Ads serves iOS and Android only. Its official FAQ supports no "
        "Windows desktop, and there is no official web or Python SDK."
    )
    _source = "https://docs.unity.com/en-us/grow/ads/references/faq"


class AppLovinProvider(_UnavailableProvider):
    name = "applovin"
    display_name = "AppLovin"
    _reason = (
        "AppLovin MAX targets Android, iOS and mobile engine wrappers. It has no "
        "Windows desktop SDK, and its publisher terms require the documented "
        "integration and an accurate traffic source."
    )
    _source = "https://legal.applovin.com/policies-publishers/"


class HousePromotionProvider:
    """An optional promotion for GisCode Pro, drawn locally.

    This is not a paid advertisement and not a stand-in for one: it earns no
    revenue and is labelled as a GisCode promotion. It exists so the panel has
    something honest to render while no third-party provider can serve Windows.
    """

    name = "house"
    display_name = "GisCode promotion"

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(available=True, reason="A local GisCode promotion.")

    def creative(self) -> Optional[AdCreative]:
        return AdCreative(
            provider=self.name,
            headline="GisCode Pro",
            body=(
                "The terminal, iOS/xtool, GisLogo, Developer & Release, Device "
                "Preview, Generate Video and Project Doctor tools are part of "
                "GisCode Pro."
            ),
            disclosure="GisCode promotion - not a paid advertisement.",
        )


#: Every provider this build knows about, in display order.
PROVIDERS: tuple[AdProvider, ...] = (
    UnityAdsProvider(),
    AppLovinProvider(),
    HousePromotionProvider(),
)

#: Provider name -> adapter.
PROVIDER_BY_NAME: dict[str, AdProvider] = {provider.name: provider for provider in PROVIDERS}

#: The value of ``ads.provider`` that means "no provider selected".
NO_PROVIDER = "none"
