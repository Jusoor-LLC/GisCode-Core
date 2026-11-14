# Integration status — Microsoft Advertising and Oro

**Date:** 2026-11-13
**Scope:** GisCode Core only. No Pro installer, signing, publication or deployment.

This note records two integration decisions with their evidence, so a later
change does not have to guess why a provider is unavailable or why an "Oro"
dependency is absent.

## Microsoft Advertising

### What was verified

* *Microsoft Advertising* (formerly Bing Ads) is the **advertiser** platform:
  campaigns are bought through it and the Bing Ads API is used to manage them.
  Its API authentication uses a Microsoft account / Entra ID application.
* The *Microsoft Advertising SDK* that displayed ads in applications is
  **UWP-only** (`Microsoft.Advertising`, `Microsoft.Advertising.WinRT.UI`) and
  its ad-monetization platform for Windows UWP apps was **shut down on
  1 June 2020**.
* A Python/Tk Win32 desktop application is neither UWP nor Windows App SDK, and
  no supported Win32 ad-serving SDK exists for it.

Sources:

* <https://learn.microsoft.com/en-us/uwp/api/overview/advertising> — "APIs you
  can use to show banner, interstitial, and native ads in your Universal
  Windows Platform (UWP) apps" (page updated 2025-08-06).
* <https://marketplace.visualstudio.com/items?itemName=AdMediator.MicrosoftAdvertisingSDK>
  — "As of June 1st 2020, the Microsoft Ad Monetization platform for Windows
  UWP apps will be shut down."
* <https://learn.microsoft.com/en-us/advertising/guides/get-started-csharp?view=bingads-13>
  — the Bing Ads API is for campaign management and authenticates through
  OAuth; it is not an ad-serving SDK.

### What was implemented

`giscode/ads/providers.py` now includes `MicrosoftAdvertisingProvider`:

* `name = "microsoft_advertising"`, `display_name = "Microsoft Advertising"`;
* `availability()` returns **unavailable** with the advertiser/UWP/retirement
  reason and a Microsoft source URL;
* `creative()` returns `None` — it never fabricates an ad;
* it is listed in `PROVIDERS`, so the AdSpace panel and settings can report it.

The adapter deliberately imports no identity SDK. **Entra ID is not an ad
server**: it is the identity layer used to authorize the advertiser API. No
network request is made anywhere in the ad path, and the existing consent,
`ads.enabled`, Pro-ad-free and fail-safe rules are unchanged.

Regression coverage: `tests/test_adspace_integration.py`
(`test_microsoft_advertising_is_a_publisher_blocker_not_entra`,
`test_microsoft_advertising_is_not_a_core_or_pro_dependency`, and the provider
inventory test).

## Oro

### Evidence

On 2026-11-13, in `Jusoor-LLC/GisCode-Core` at the current working revision:

| Check | Command | Result |
| --- | --- | --- |
| Source/docs/config mention | `grep -rniw "oro" --include=*.py --include=*.md --include=*.yml --include=*.json --include=*.toml .` | **0 matches** |
| Commit history | `git log --all --oneline --grep="Oro" -i` | **0 commits** |
| Issues | `gh issue list --repo Jusoor-LLC/GisCode-Core --state all` | **0 issues** |
| Pull requests | `gh pr list --repo Jusoor-LLC/GisCode-Core --state all` | no Oro PR |
| Runtime/build dependencies | `grep -rinw "oro" requirements.txt requirements-lock.txt pyproject.toml` | **0 matches** |

### Conclusion — blocked, not silently implemented

There is no Oro integration, dependency, endpoint, credential, schema, issue or
history in the repository. "Oro" is therefore ambiguous: it could mean
OroPlatform/OroCommerce (a PHP B2B platform with a REST API), an internal
service, or a vendor product. Implementing anything against it now would mean
inventing an endpoint and an authentication scheme — unverifiable code that
could leak credentials or silently do nothing.

**Unblock requirements (owner input needed):**

1. which Oro product and environment (host/base URL);
2. the API or event contract to integrate (e.g. REST resources, webhooks);
3. authentication method and a sandbox credential supplied outside the repo;
4. the Core behaviour it must drive (sync projects, tasks, customers, or
   something else).

Until those exist there is no code change to make; this is recorded as an
explicit blocker rather than a guessed integration. No placeholder module was
added, and no credential or endpoint appears in the tree.
