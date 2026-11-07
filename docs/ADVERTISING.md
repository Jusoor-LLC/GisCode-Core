# GisCode AdSpace

Core has one advertising surface: its own **AdSpace** tab. It never overlays
Chat, Projects, model selection or attachment analysis, it renders text only,
and Pro has no advertising at all — the panel is not built and the service
reports the Pro edition as ad-free.

## Status: provider integration BLOCKED

The two providers the owner asked for were checked against their current
official documentation (October 2026). **Neither supports Windows desktop**, and
neither offers an official web or Python SDK, so neither can legitimately serve
GisCode Core on Windows. Nothing was registered, no account was created, and no
monetisation was activated.

### Unity Ads

- Platform list from the official FAQ: *"Unity Ads is an ad network for **iOS
  (iTunes) and Android (Google Play)**"*. The same FAQ lists what it does not
  support, including WebGL/WebPlayer; Windows desktop is not a supported target
  at all.
- The Ads Mediation package for Unity 6000.7 states
  `Supported platforms: Android, iOS`.
- Integration paths are the Unity C# package, an Android AAR and an iOS
  framework. There is no Windows DLL, no .NET binding and no Python binding.
- Sources: <https://docs.unity.com/en-us/grow/ads/references/faq>,
  <https://docs.unity.com/en-us/engine/6000.7/manual/packages-list/packages-all/pack-safe/com-unity-services-levelplay>,
  <https://docs.unity.com/en-us/grow/ads/process-overview>
- Registration would need a Unity organisation, a project and per-platform Game
  IDs; payouts are monthly, USD only, net 60, through a payout profile. None of
  that applies without a supported platform.

### AppLovin (MAX)

- Documented SDK targets are Android (including Amazon Fire OS), iOS, and the
  engine wrappers that build to those: Unity, Adobe AIR, Unreal, Cocos2d-x,
  React Native, Flutter, .NET/MAUI, Godot, Cordova. A Windows EXE has no
  documented integration.
- The publisher policies require integration *"as described in the AppLovin
  technical integration documentation"* and require that *"the actual traffic
  source must match the disclosed traffic source"*. Serving MAX inventory from
  a Windows/Python app would misrepresent the inventory.
- The EULA limits the SDK licence to use *"solely for purposes of your access to
  and use of the Platform and the Services"*, so wrapping it in a desktop shell
  is outside the licence as well as technically unsupported.
- Sources: <https://legal.applovin.com/policies-publishers/>,
  <https://legal.applovin.com/eula/>

### What would unblock it

A provider that explicitly supports Windows desktop, or one whose terms permit a
documented desktop/web integration. Candidates were not integrated and are not
recommended here; choosing one is an owner decision that also needs a privacy
and consent review.

## How the code is arranged

```
giscode/ads/providers.py   AdProvider protocol, UnityAds/AppLovin adapters,
                           optional local house promotion
giscode/ads/service.py     AdSpace: the rules, as one answer per call
giscode/gui/adspace.py     AdSpacePanel: draws that answer, nothing else
```

`AdSpace.state()` decides, in order:

1. Pro → `ad_free`, no provider consulted, no creative.
2. `ads.enabled` false → nothing.
3. `ads.consent` false → nothing, and the consent checkbox gates the rest.
4. no provider configured → nothing, with the mobile-only explanation.
5. provider unavailable → nothing, with that provider's reason and source.
6. otherwise → one text creative, with a mandatory disclosure line.

It never raises: an unreadable settings file, an unknown provider name or a
provider that throws all produce "no ad". It performs no network request and
opens no socket — a test patches `socket.socket` to fail and the service still
answers.

## Adding a provider

Implement `AdProvider` (`name`, `display_name`, `availability()`,
`creative()`) and add it to `PROVIDERS`. `availability()` must return
`ProviderAvailability(available=False, reason=..., source=...)` when the
platform is unsupported — never claim availability to make the panel look full.
If an adapter ever performs a request, gate it on `ads.enabled` **and**
`ads.consent`, and keep the creative text-only so no remote markup or script can
enter the application.

## Settings

| Key | Default | Meaning |
| --- | --- | --- |
| `ads.enabled` | `false` | Master switch; off means no request and no creative. |
| `ads.consent` | `false` | The user's consent to receive ad content. |
| `ads.provider` | `none` | `none`, `unity_ads`, `applovin`, or `house`. |

`house` renders a local GisCode Pro promotion. It earns nothing and is labelled
"GisCode promotion - not a paid advertisement". It exists so the panel can show
a real, honest state while no third-party provider can serve Windows; it is not
a stand-in for monetisation.

## What is actually possible in a Windows desktop app

Checked against the providers' own policies in October 2026:

| Option | Desktop app? | Evidence |
| --- | --- | --- |
| Unity Ads | No | mobile only; no Windows SDK |
| AppLovin (MAX) | No | Android/iOS and mobile engine wrappers only |
| Google AdSense / Ad Manager | Forbidden | *"Google ads ... may not be integrated into a software application of any kind"* (<https://support.google.com/adsense/answer/48182>) |
| EthicalAds | No | website/newsletter placements, invite-only, ~$2.50 CPM, $50 minimum payout (<https://www.ethicalads.io/publishers/>) |
| Direct sponsor, sold per release | Yes | a first-party text creative, paid by invoice; no network, no tracking |
| GitHub Sponsors | n/a | funding, not advertising; Egypt is a supported region, 0% fee on personal sponsorships (<https://docs.github.com/en/sponsors/getting-started-with-github-sponsors/about-github-sponsors>) |

So there is no advertising network that may legitimately serve this application.
The two honest paths are a **directly sold sponsor slot** (rendered through the
same adapter interface, as a text creative with the disclosure line) and
**funding rather than advertising** (GitHub Sponsors, and Pro licence sales).
Both need a published release and an audience first; until then advertising
stays off and the panel says so.

## Owner actions

1. Decide whether a desktop-capable provider, or a published web property that
   the provider supports, is worth pursuing.
2. If yes, register the developer account with the address you keep for
   registrations on that provider's official site, entering the password
   privately — never in chat, source, configuration or logs.
3. Complete e-mail verification, CAPTCHA or 2FA personally; no agent can do
   that step.
4. Provide only the non-secret placement identifier the provider issues; keep
   credentials out of the repository.
5. Approve enabling advertising; it stays off until then.
