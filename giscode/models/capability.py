"""What a model can do, and how strongly that is known.

Today's registry stores a flat ``capabilities`` list. A capability that came
from a catalog entry, one guessed from a file name and one established by a
test are indistinguishable in that list, so nothing downstream can tell a
declaration from a guess - and Stage 3G's coding gate inherited that weakness
(``compute_capabilities_for_discovered`` marks a file ``coding`` when its name
contains "coder").

This module keeps the existing list working and adds the missing distinction:

* :data:`EVIDENCE_LEVELS` - an ordered set of levels, from a file-name guess to
  an independent verification.
* :class:`ModelIdentity` - a content identity that survives a rename or a move
  and changes when the weights do, so a verification is never carried over to a
  different file. It has two halves: a header digest, which is only a cheap
  index key, and a full-file SHA-256 (:func:`content_identity_for`), which is
  what a persisted verification is anchored to. The header alone cannot see a
  change made to tensor data past its first few megabytes, so it is never
  treated as proof of content.
* :func:`evidence_for` - one evidence record per capability, derived from what
  is actually known about *this* model, merged with any stored record.

It deliberately does not decide suitability: a capability record says nothing
about memory or the runtime (see ``giscode.models.feasibility`` and
``giscode.inference.resource_budget`` for those).
"""
from __future__ import annotations

import hashlib
import itertools
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

#: Evidence levels, weakest first. The order is the strength order.
UNKNOWN = "unknown"
INFERRED_NAME = "inferred_name"
METADATA = "metadata"
DECLARED = "declared"
USER_CONFIRMED = "user_confirmed"
VERIFIED = "verified"

EVIDENCE_LEVELS = (UNKNOWN, INFERRED_NAME, METADATA, DECLARED, USER_CONFIRMED, VERIFIED)

_STRENGTH = {level: index for index, level in enumerate(EVIDENCE_LEVELS)}

#: Levels a claim of "this model really does X" may rest on. A file-name guess
#: is usable as a hint but is never evidence.
TRUSTWORTHY_LEVELS = frozenset({METADATA, DECLARED, USER_CONFIRMED, VERIFIED})

#: Levels that may be written to the store (the ones a person or a test
#: established, rather than something re-derivable from the file).
PERSISTED_LEVELS = frozenset({USER_CONFIRMED, VERIFIED})

#: How much of a model file the *header* digest reads. Enough to include the
#: GGUF header and its tensor table, small enough to hash a 30B file instantly.
#: It is not a full-file hash: it cannot see a change made to tensor data
#: further in, which is why it is only an index key and never a trust anchor.
_IDENTITY_BYTES = 8 * 1024 * 1024

#: One read at a time when hashing a whole file. Memory use is this constant
#: whatever the model weighs.
_CONTENT_CHUNK = 4 * 1024 * 1024

_IDENTITY_CACHE: dict[tuple, "ModelIdentity"] = {}
_CONTENT_CACHE: dict[tuple, "ModelIdentity"] = {}
_IDENTITY_LOCK = threading.Lock()
_IDENTITY_CACHE_LIMIT = 256

#: Handed out to stamps whose change time could not be read, so those stamps can
#: never compare equal to each other and no cache may reuse a stale hash.
_UNSTABLE_STAMPS = itertools.count(1)

#: ``FSCTL_READ_FILE_USN_DATA``: the file's update sequence number on NTFS.
_FSCTL_READ_FILE_USN_DATA = 0x000900EB
_USN_BUFFER_BYTES = 1024

_WINDOWS_USN_READER = None


def _windows_usn_reader():
    """``(kernel32, ctypes, wintypes)`` on Windows, else ``None``.

    Resolved once. The USN is the only change marker ``os.stat`` cannot report
    on Windows, and it is needed because two weaker candidates do not work:

    * ``st_ctime`` there is the creation time, which never moves;
    * the ``ChangeTime`` in ``FILE_BASIC_INFO`` is only as precise as the
      system clock tick - measured on this machine, a rewrite followed by a
      restored mtime left it identical 54 times in 200.

    The USN increments on every write and is not settable by ``os.utime``.
    """
    global _WINDOWS_USN_READER
    if _WINDOWS_USN_READER is not None:
        return _WINDOWS_USN_READER or None
    if os.name != "nt":
        _WINDOWS_USN_READER = False
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        _WINDOWS_USN_READER = (kernel32, ctypes, wintypes)
    except Exception:  # pragma: no cover - platform glue, never fatal
        _WINDOWS_USN_READER = False
        return None
    return _WINDOWS_USN_READER


def _windows_change_token(path: str) -> Optional[str]:
    """The file's update sequence number, or ``None`` when it cannot be read.

    ``None`` on any volume without USNs (FAT32, exFAT, some network shares), and
    the caller then refuses to cache rather than guessing.
    """
    reader = _windows_usn_reader()
    if reader is None:
        return None
    kernel32, ctypes, wintypes = reader
    try:
        import msvcrt

        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        handle = msvcrt.get_osfhandle(descriptor)
        buffer = ctypes.create_string_buffer(_USN_BUFFER_BYTES)
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            handle,
            _FSCTL_READ_FILE_USN_DATA,
            None,
            0,
            buffer,
            _USN_BUFFER_BYTES,
            ctypes.byref(returned),
            None,
        ):
            return None
        if returned.value < 32:  # USN_RECORD_V2 puts the USN at offset 24
            return None
        return "usn:%d" % int.from_bytes(buffer.raw[24:32], "little", signed=True)
    except Exception:  # pragma: no cover - platform glue, never fatal
        return None
    finally:
        os.close(descriptor)


def _change_token(path: str, stat) -> tuple:
    """A token that moves whenever the file is rewritten, if one can be read.

    POSIX exposes the inode change time as ``st_ctime_ns``, which an ordinary
    writer cannot restore. Windows needs its update sequence number (see
    :func:`_windows_usn_reader`). Where neither is available the token is unique
    per call, which makes every stamp differ and forces a recomputation rather
    than risking a cached digest for content that may have changed under an
    unchanged path, size and mtime.
    """
    if os.name == "nt":
        token = _windows_change_token(path)
        if token is not None:
            return (token,)
        return ("unstable", next(_UNSTABLE_STAMPS))
    return (int(getattr(stat, "st_ctime_ns", 0)),)


def level_strength(level: str) -> int:
    return _STRENGTH.get(str(level).strip().lower(), 0)


def stronger(left: str, right: str) -> str:
    """The stronger of two evidence levels."""
    return left if level_strength(left) >= level_strength(right) else right


def is_confirmed(level: str) -> bool:
    """True when a person or a test vouched for the capability.

    A user's statement is worth acting on, but it is not independent evidence:
    it is ``user_confirmed``, never ``verified``.
    """
    return level_strength(level) >= level_strength(USER_CONFIRMED)


def is_verified(level: str) -> bool:
    """True only for an independent verification.

    ``declared`` and ``metadata`` are trusted *sources*, but neither proves the
    model does the job; a ``user_confirmed`` claim is a person's word, not a
    test. Only ``verified`` means "this was checked".
    """
    return level_strength(level) >= level_strength(VERIFIED)


def is_trustworthy(level: str) -> bool:
    """True when the level is better than a guess (>= ``metadata``)."""
    return str(level).strip().lower() in TRUSTWORTHY_LEVELS


@dataclass(frozen=True)
class ModelIdentity:
    """A path-independent identity for one model file.

    ``digest`` is a SHA-256 over the first :data:`_IDENTITY_BYTES` of the file
    plus its exact size: a *header* fingerprint. It is only an index key. It is
    cheap enough to compute for every model at discovery, it survives a rename
    or a move, and a different model, a different quantization or a truncated
    download changes it.

    It is deliberately **not** a full-file hash and proves nothing about the
    tensor data: a merged fine-tune or a re-quantization that keeps the size and
    the first few megabytes can leave it identical. That is why it is never the
    anchor for a persisted capability decision. :func:`content_identity_for`
    fills in ``content`` - a SHA-256 over the *whole* file - and only an
    identity with ``complete`` true and ``content`` set may carry a verification
    (see :attr:`trusted`).
    """

    digest: str = ""
    size_bytes: int = 0
    #: SHA-256 of the entire file. Empty until :func:`content_identity_for`
    #: reads it, and never derived from the header alone.
    content: str = ""
    #: True only when ``content`` was computed by reading every byte.
    complete: bool = False
    #: What was read, so a report can never describe a partial check as a
    #: full-file one.
    detail: str = ""

    @property
    def known(self) -> bool:
        return bool(self.digest)

    @property
    def trusted(self) -> bool:
        """True only for an identity computed from the whole file."""
        return bool(self.digest and self.content and self.complete)

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "content": self.content,
            "complete": self.complete,
            "detail": self.detail,
        }


def _stamp(path: str) -> Optional[tuple]:
    """The file's identity-and-metadata stamp, or ``None`` when it is missing.

    Size and mtime alone do not establish that content is unchanged (a writer
    can put the mtime back), so the stamp also carries the platform's change
    time and file identity. On POSIX the inode change time is not restorable by
    an ordinary writer; on Windows it is read from the file's basic information
    instead, because ``st_ctime`` there is the creation time. Where no change
    time can be read, the stamp is made unique per call so nothing is cached.

    It is still not proof. The hash is recomputed whenever the stamp differs,
    and a record is only trusted after the whole file has been hashed again.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (
        os.path.normcase(path),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
    ) + _change_token(path, stat)


def _unchanged(path: str, stamp: tuple) -> bool:
    """False when the file changed while it was being read.

    Hashing a file that is being replaced would otherwise produce a digest that
    matches neither version, so the identity is reported as unknown and
    recomputed on the next call. That is what stops a partial download from
    earning a trusted identity.
    """
    return _stamp(path) == stamp


def content_identity_for(path: "str | os.PathLike[str]") -> ModelIdentity:
    """Full identity of *path*, computed from every byte of the file.

    The header digest is computed first (it is the index key, and it is cached),
    then the file is read in fixed chunks and hashed. Memory use is one chunk,
    whatever the model weighs, so a 20 GB file is hashed without being loaded.

    A file that cannot be read, or that changes while it is being read, returns
    an unknown identity rather than a digest of a torn read. Results are cached
    for the life of the process against the file's full stamp *and* its header
    digest, so repeated lookups of one unchanged file hash it once; any change
    to the stamp or the header recomputes it.

    No persistent cache of this digest is written. A stored hash cannot prove
    that a file still holds the same bytes, so a later process would have to
    trust metadata alone to reuse it - which is the mistake this function exists
    to prevent.
    """
    text = str(path or "")
    if not text:
        return ModelIdentity()
    stamp = _stamp(text)
    if stamp is None:
        return ModelIdentity()
    header = identity_for(text)
    if not header.known:
        return ModelIdentity()
    key = stamp + (header.digest,)
    with _IDENTITY_LOCK:
        cached = _CONTENT_CACHE.get(key)
    if cached is not None:
        return cached
    hasher = hashlib.sha256()
    try:
        with open(text, "rb") as handle:
            while True:
                chunk = handle.read(_CONTENT_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError:
        return ModelIdentity()
    if not _unchanged(text, stamp):
        return ModelIdentity()
    identity = ModelIdentity(
        digest=header.digest,
        size_bytes=stamp[1],
        content="sha256:" + hasher.hexdigest(),
        complete=True,
        detail="the whole file",
    )
    with _IDENTITY_LOCK:
        if len(_CONTENT_CACHE) >= _IDENTITY_CACHE_LIMIT:
            _CONTENT_CACHE.clear()
        _CONTENT_CACHE[key] = identity
    return identity


def identity_for(path: "str | os.PathLike[str]") -> ModelIdentity:
    """Header identity of *path*, or an unknown identity when it cannot be read.

    This is the cheap index key, not a content identity: use
    :func:`content_identity_for` before trusting anything a record says.
    """
    text = str(path or "")
    if not text:
        return ModelIdentity()
    stamp = _stamp(text)
    if stamp is None:
        return ModelIdentity()
    with _IDENTITY_LOCK:
        cached = _IDENTITY_CACHE.get(stamp)
    if cached is not None:
        return cached
    digest = ""
    try:
        hasher = hashlib.sha256()
        with open(text, "rb") as handle:
            hasher.update(handle.read(_IDENTITY_BYTES))
        hasher.update(b"|size=%d" % int(stamp[1]))
        digest = "sha256:" + hasher.hexdigest()
    except OSError:
        digest = ""
    if not digest or not _unchanged(text, stamp):
        return ModelIdentity()
    identity = ModelIdentity(digest=digest, size_bytes=int(stamp[1]))
    with _IDENTITY_LOCK:
        if len(_IDENTITY_CACHE) >= _IDENTITY_CACHE_LIMIT:
            _IDENTITY_CACHE.clear()
        _IDENTITY_CACHE[stamp] = identity
    return identity


@dataclass(frozen=True)
class CapabilityEvidence:
    """One capability and the strength of the evidence for it."""

    capability: str
    level: str
    source: str
    detail: str = ""

    @property
    def verified(self) -> bool:
        return is_verified(self.level)

    @property
    def confirmed(self) -> bool:
        """A person or a test vouched for it (``user_confirmed`` or better)."""
        return is_confirmed(self.level)

    @property
    def trustworthy(self) -> bool:
        return is_trustworthy(self.level)

    def as_dict(self) -> dict:
        return {
            "capability": self.capability,
            "level": self.level,
            "source": self.source,
            "detail": self.detail,
            "verified": self.verified,
            "confirmed": self.confirmed,
        }

    def describe(self) -> str:
        label = {
            VERIFIED: "verified",
            USER_CONFIRMED: "confirmed by the user",
            DECLARED: "declared by a catalog entry",
            METADATA: "supported by the model's own metadata",
            INFERRED_NAME: "inferred from the file name",
            UNKNOWN: "unknown",
        }.get(self.level, self.level)
        if self.detail:
            return f"{self.capability}: {label} ({self.detail})"
        return f"{self.capability}: {label}"


def _normalised_capabilities(metadata) -> list[str]:
    values = list(getattr(metadata, "capabilities", []) or [])
    seen: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if text and text not in seen:
            seen.append(text)
    return seen


def _came_from_catalog(metadata) -> bool:
    """Whether this metadata was declared rather than guessed from a file.

    Discovery stamps ``source="discovered"`` (or leaves the historical
    ``family="discovered"``); a catalog entry carries its real family.
    """
    source = str(getattr(metadata, "source", "") or "").strip().lower()
    if source:
        return source not in {"discovered", "unknown"}
    family = str(getattr(metadata, "family", "") or "").strip().lower()
    return bool(family) and family not in {"discovered", "unknown"}


def _is_readable_file(path: str) -> bool:
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _name_tokens(value: str) -> set[str]:
    tokens = {token for token in str(value or "").lower().replace("_", "-").split("-") if token}
    return {token for token in tokens if len(token) >= 3 and not token.isdigit()}


def _mmproj_evidence(path: str) -> tuple[bool, str]:
    """Whether a projector beside *path* belongs to *this* model.

    A projector in the same folder used to be enough evidence for every model
    in that folder, which granted vision to unrelated models sharing a download
    directory. It now has to share a name token with the model, or be the only
    projector in a folder holding nothing but this one model.
    """
    if not path:
        return False, "the model has no file to inspect"
    try:
        parent = Path(str(path)).parent
        entries = [item for item in parent.iterdir() if item.is_file()]
    except (OSError, ValueError):
        return False, "the model's folder could not be read"
    projectors = [item for item in entries if "mmproj" in item.name.lower()]
    if not projectors:
        return False, "no projector sits beside the model"
    model_tokens = _name_tokens(Path(str(path)).stem)
    for candidate in projectors:
        # Two shared tokens, not one: models of the same family ("Qwen3") would
        # otherwise claim each other's projectors.
        if len(model_tokens & _name_tokens(candidate.stem)) >= 2:
            return True, f"{candidate.name} shares this model's name"
    others = [
        item
        for item in entries
        if item.suffix.lower() == ".gguf" and "mmproj" not in item.name.lower()
        and item.name.lower() != Path(str(path)).name.lower()
    ]
    if len(projectors) == 1 and not others:
        return True, f"{projectors[0].name} is the only projector and this is the only model here"
    return (
        False,
        "a projector is in the folder but its name does not match this model, "
        "so it is not evidence about it",
    )


def _vision_name_hint(name: str, tags=()) -> bool:
    """Whether the model's name or tags suggest vision (reusing the one hint list)."""
    try:
        from giscode.vision.capabilities import is_vision_capable

        return bool(
            is_vision_capable(
                type(
                    "_NameOnly",
                    (),
                    {"name": name, "tags": list(tags or []), "capabilities": [], "path": ""},
                )()
            )
        )
    except Exception:  # pragma: no cover - a hint helper must never break evidence
        return False


def _stored_level(stored: Mapping[str, Mapping] | None, capability: str) -> Optional[CapabilityEvidence]:
    if not stored:
        return None
    raw = stored.get(capability)
    if not isinstance(raw, Mapping):
        return None
    level = str(raw.get("level") or UNKNOWN).strip().lower()
    if level not in PERSISTED_LEVELS:
        # A stored record may only strengthen a capability; anything else is
        # re-derived from the file so a hand-edited file cannot weaken it.
        return None
    return CapabilityEvidence(
        capability=capability,
        level=level,
        source=str(raw.get("source") or "stored record"),
        detail=str(raw.get("detail") or ""),
    )


def evidence_for(
    metadata,
    stored: Optional[Mapping[str, Mapping]] = None,
) -> dict[str, CapabilityEvidence]:
    """One :class:`CapabilityEvidence` per declared capability.

    ``stored`` is the persisted record for this model's identity (see
    :mod:`giscode.models.capability_store`); it can only raise a level, never
    lower one, so a stale record cannot claim less than the file proves. It may
    also *add* a capability the file cannot reveal - a person who confirms that
    a model without a "coder" name hint writes code - but only at a level the
    store may hold (``user_confirmed`` or ``verified``), and a confirmation is
    never reported as a verification.
    """
    path = str(getattr(metadata, "path", "") or "")
    declared = _came_from_catalog(metadata)
    name = str(getattr(metadata, "name", "") or "").lower()
    records: dict[str, CapabilityEvidence] = {}

    for capability in _normalised_capabilities(metadata):
        base: CapabilityEvidence
        if declared:
            base = CapabilityEvidence(
                capability=capability,
                level=DECLARED,
                source="catalog",
                detail=f"declared by the catalog entry for {getattr(metadata, 'family', '') or 'this model'}",
            )
        elif capability in {"text", "chat"}:
            # A readable GGUF chat file really is a text model; the file format
            # is the evidence, not its name. A path that ends in .gguf but does
            # not exist proves nothing.
            readable = path.lower().endswith(".gguf") and _is_readable_file(path)
            base = CapabilityEvidence(
                capability=capability,
                level=METADATA if readable else UNKNOWN,
                source="gguf",
                detail="the file is a readable GGUF chat model" if readable else "no readable model file",
            )
        elif capability == "coding":
            # This is the Stage 3G gap: for a discovered file the only evidence
            # is its name, which is a hint and never a verification.
            base = CapabilityEvidence(
                capability=capability,
                level=INFERRED_NAME if ("coder" in name or "code" in name) else UNKNOWN,
                source="filename",
                detail="the file name contains a coding hint",
            )
        elif capability == "vision":
            matched, mmproj_detail = _mmproj_evidence(path)
            if matched:
                base = CapabilityEvidence(
                    capability=capability,
                    level=METADATA,
                    source="mmproj",
                    detail=mmproj_detail,
                )
            elif _vision_name_hint(name, getattr(metadata, "tags", None)):
                base = CapabilityEvidence(
                    capability=capability,
                    level=INFERRED_NAME,
                    source="filename",
                    detail="the file name contains a vision hint",
                )
            else:
                base = CapabilityEvidence(
                    capability=capability,
                    level=UNKNOWN,
                    source="none",
                    detail=mmproj_detail,
                )
        else:
            base = CapabilityEvidence(
                capability=capability,
                level=INFERRED_NAME,
                source="name",
                detail="derived from the model's name",
            )
        persisted = _stored_level(stored, capability)
        if persisted is not None and level_strength(persisted.level) > level_strength(base.level):
            base = persisted
        records[capability] = base

    # A record can also establish a capability the model file itself cannot
    # reveal: a discovered file with no coding hint in its name whose owner has
    # confirmed it writes code. Without this the confirmation was silently
    # dropped, because the derived list never contained the capability to
    # strengthen. Only a persisted decision qualifies (``_stored_level`` refuses
    # everything weaker), so a guess can still never appear this way.
    if stored:
        for capability in stored:
            key = str(capability or "").strip().lower()
            if not key or key in records:
                continue
            persisted = _stored_level(stored, key)
            if persisted is not None:
                records[key] = persisted
    return records


def evidence_for_capability(
    metadata,
    capability: str,
    stored: Optional[Mapping[str, Mapping]] = None,
) -> Optional[CapabilityEvidence]:
    """Evidence for one capability, or ``None`` when the model does not claim it."""
    return evidence_for(metadata, stored).get(str(capability or "").strip().lower())


def summary(records: Mapping[str, CapabilityEvidence] | Iterable[CapabilityEvidence]) -> dict[str, dict]:
    """A plain ``{capability: {...}}`` mapping, safe to store on metadata."""
    if isinstance(records, Mapping):
        items = records.values()
    else:
        items = records
    return {record.capability: record.as_dict() for record in items}


def describe_all(records: Mapping[str, CapabilityEvidence] | Iterable[CapabilityEvidence]) -> str:
    items = records.values() if isinstance(records, Mapping) else records
    return "; ".join(record.describe() for record in items)
