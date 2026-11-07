"""GisCode AdSpace: the advertising surface of the free Core edition.

See :mod:`giscode.ads.providers` for why Unity Ads and AppLovin are not
integrated on Windows, and :mod:`giscode.ads.service` for the rules.
"""
from giscode.ads.providers import (
    NO_PROVIDER,
    PROVIDERS,
    PROVIDER_BY_NAME,
    AdCreative,
    AdProvider,
    AppLovinProvider,
    HousePromotionProvider,
    ProviderAvailability,
    UnityAdsProvider,
)
from giscode.ads.service import AdSpace, AdSpaceState, ProviderReport, provider_names

__all__ = [
    "NO_PROVIDER",
    "PROVIDERS",
    "PROVIDER_BY_NAME",
    "AdCreative",
    "AdProvider",
    "AdSpace",
    "AdSpaceState",
    "AppLovinProvider",
    "HousePromotionProvider",
    "ProviderAvailability",
    "ProviderReport",
    "UnityAdsProvider",
    "provider_names",
]
