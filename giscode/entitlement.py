"""Provider-neutral Pro entitlement contract.

This module deliberately contains **no licensing provider, no credentials and no
network access**. It defines what a verified entitlement is, how a caller asks
for one, and how the application must behave when none exists.

Why it exists: ``GISCODE_EDITION`` selects which surfaces are built. It is a
launch preference, not proof that somebody paid for Pro, and an environment
variable must never be mistaken for a licence. Previously the only gate was
``edition.is_pro()``; that gate is still required (a Core launch must never
reach Pro capability code) but it is *not sufficient*.

The contract is intentionally small so a real provider can be added later
without changing the service:

* :class:`EntitlementProvider` — one method, ``check()``, returning a verdict.
* :class:`EntitlementCheck` — the verdict, with a status and a short detail.
* :class:`Entitlement` — what a verified entitlement describes.

Every status other than ``VERIFIED`` fails closed. A provider that raises is
treated as ``UNAVAILABLE``; a missing provider is ``MISSING``; nothing is
inferred from the edition variable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable


class EntitlementStatus(str, Enum):
    """The outcome of asking whether this installation is licensed for Pro."""

    VERIFIED = "verified"
    MISSING = "missing"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Entitlement:
    """A verified Pro entitlement.

    ``source`` names the provider that verified it, so a log can say *what*
    established the entitlement without recording any credential material.
    """

    subject: str = ""
    edition: str = "pro"
    features: frozenset[str] = frozenset()
    expires_at: str = ""
    source: str = ""

    def covers(self, feature: str) -> bool:
        """True when the entitlement names *feature* (empty means "all Pro")."""

        return not self.features or str(feature or "") in self.features

    def expired(self, *, now: datetime | None = None) -> bool:
        """True when ``expires_at`` is set, parseable and in the past."""

        if not self.expires_at:
            return False
        try:
            moment = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True  # an unparseable expiry is not a valid entitlement
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment <= (now or datetime.now(tz=timezone.utc))


@dataclass(frozen=True)
class EntitlementCheck:
    """The verdict of one entitlement check."""

    status: EntitlementStatus = EntitlementStatus.MISSING
    detail: str = ""
    entitlement: Entitlement | None = None

    @property
    def verified(self) -> bool:
        """True only for a verified, unexpired entitlement."""

        if self.status is not EntitlementStatus.VERIFIED:
            return False
        return not (self.entitlement is not None and self.entitlement.expired())


@runtime_checkable
class EntitlementProvider(Protocol):
    """What a licensing provider must implement. No I/O is mandated here."""

    def check(self) -> EntitlementCheck:
        ...


@dataclass(frozen=True)
class NullEntitlementProvider:
    """The provider used when the build has none: always MISSING.

    This is not a permissive default. It exists so the service has something
    explicit to consult that can only ever answer "no entitlement".
    """

    detail: str = (
        "no entitlement provider is configured in this build; Pro licensing is "
        "not enforced and Pro execution is unavailable"
    )

    def check(self) -> EntitlementCheck:
        return EntitlementCheck(EntitlementStatus.MISSING, self.detail)


@dataclass(frozen=True)
class StaticEntitlementProvider:
    """A deterministic provider for tests and local fixtures.

    It is named ``Static`` because it decides nothing: the caller supplies the
    verdict. It must not be used to stand in for a commercial licence, and it
    never appears in the application's startup path.
    """

    status: EntitlementStatus = EntitlementStatus.VERIFIED
    entitlement: Entitlement | None = None
    detail: str = "deterministic test entitlement"

    def check(self) -> EntitlementCheck:
        if self.status is not EntitlementStatus.VERIFIED:
            return EntitlementCheck(self.status, self.detail, None)
        return EntitlementCheck(
            EntitlementStatus.VERIFIED,
            self.detail,
            self.entitlement or Entitlement(subject="test", source="fixture"),
        )


def describe(check: EntitlementCheck) -> str:
    """One line for a log or an about box: never contains credential material."""

    status = check.status.value
    if check.verified:
        source = check.entitlement.source if check.entitlement else "unknown"
        return f"Pro entitlement verified by {source}"
    if check.status is EntitlementStatus.VERIFIED:
        # The provider answered "verified", but the entitlement is expired or
        # unusable. Repeating "verified" here would be the exact conflation this
        # module exists to prevent.
        return "Pro entitlement expired or unusable"
    return f"Pro entitlement {status}: {check.detail or 'not established'}"
