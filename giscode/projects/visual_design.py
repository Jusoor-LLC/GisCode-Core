"""JSON-serializable visual-design document primitives.

This is the project-owned model for the native Visual App Design workspace.
Screen IDs are stable relationship keys; display names are editable labels.
The per-screen ``properties`` mapping is intentionally an extensibility point
for later bounded visual-design features.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Optional
import uuid


SCREEN_TYPES = (
    "Generic",
    "Splash / Startup",
    "Login",
    "Register",
    "Dashboard",
    "Profile",
    "Settings",
    "Search",
    "Notifications",
    "Forms",
    "List",
    "Details",
)
DEFAULT_SCREEN_TYPE = "Generic"

SHAPES = ("rounded", "sharp", "pill", "diamond", "circle")
DEFAULT_SHAPE = "rounded"
SHAPE_OPTIONS = (
    ("rounded", "Rounded"),
    ("sharp", "Sharp"),
    ("pill", "Pill"),
    ("diamond", "Diamond"),
    ("circle", "Circle"),
)

DEFAULT_FILL_COLOR = "#FFFFFF"
DEFAULT_BORDER_COLOR = "#DAD8CE"
_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}\Z")


def _is_valid_link_record(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("label"), str)
        and isinstance(record.get("url"), str)
        and bool(record["url"].strip())
    )


def _read_valid_links(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {"label": record["label"], "url": record["url"]}
        for record in value
        if _is_valid_link_record(record)
    ]


def _validate_links(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise ValueError("visual screen links must be a list")
    validated = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"visual screen link {index} must be a dictionary")
        if not isinstance(record.get("label"), str):
            raise ValueError(f"visual screen link {index} label must be a string")
        if not isinstance(record.get("url"), str) or not record["url"].strip():
            raise ValueError(f"visual screen link {index} URL must be a non-empty string")
        validated.append({"label": record["label"], "url": record["url"]})
    return validated


# Stable JSON keys mapped to the compact Unicode glyphs used by the HTML
# reference. The UI presents friendly labels; project data stores only IDs.
ICON_GLYPHS = {
    "home": "🏠",
    "user": "👤",
    "settings": "⚙️",
    "search": "🔍",
    "notifications": "🔔",
    "dashboard": "📊",
    "list": "📋",
    "login": "🔑",
    "document": "📄",
    "help": "❓",
    "launch": "🚀",
}
ICON_OPTIONS = (
    ("", "No icon"),
    ("home", "🏠 Home"),
    ("user", "👤 User"),
    ("settings", "⚙️ Settings"),
    ("search", "🔍 Search"),
    ("notifications", "🔔 Notifications"),
    ("dashboard", "📊 Dashboard"),
    ("list", "📋 List"),
    ("login", "🔑 Login"),
    ("document", "📄 Document"),
    ("help", "❓ Help"),
    ("launch", "🚀 Launch"),
)


@dataclass
class VisualScreen:
    id: str
    name: str
    x: float = 60
    y: float = 60
    width: float = 180
    height: float = 88
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def screen_type(self) -> str:
        """Return a supported type, falling back without mutating legacy data."""
        value = self.properties.get("screen_type", DEFAULT_SCREEN_TYPE)
        return value if value in SCREEN_TYPES else DEFAULT_SCREEN_TYPE

    def set_screen_type(self, screen_type: str) -> None:
        if screen_type not in SCREEN_TYPES:
            raise ValueError(
                f"invalid screen type: {screen_type!r}; "
                f"expected one of {SCREEN_TYPES}"
            )
        self.properties["screen_type"] = screen_type

    @property
    def description(self) -> str:
        """Return the screen description, defaulting legacy screens to empty."""
        value = self.properties.get("description", "")
        return value if isinstance(value, str) else ""

    def set_description(self, description: str) -> None:
        if not isinstance(description, str):
            raise ValueError("visual screen description must be a string")
        self.properties["description"] = description

    @property
    def links(self) -> list[dict[str, Any]]:
        """Return valid detached references without mutating stored legacy data."""
        return _read_valid_links(self.properties.get("links", []))

    def set_links(self, links: list[dict[str, Any]]) -> None:
        self.properties["links"] = _validate_links(links)

    @property
    def shape(self) -> str:
        """Return a supported shape, safely defaulting without changing data."""
        value = self.properties.get("shape", DEFAULT_SHAPE)
        return value if isinstance(value, str) and value in SHAPES else DEFAULT_SHAPE

    def set_shape(self, shape: str) -> None:
        if not isinstance(shape, str) or shape not in SHAPES:
            raise ValueError(f"invalid visual screen shape: {shape!r}")
        self.properties["shape"] = shape

    @property
    def fill_color(self) -> str:
        """Return a valid fill color, falling back without mutating stored data."""
        value = self.properties.get("fill_color")
        return value if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value) else DEFAULT_FILL_COLOR

    def set_fill_color(self, color: Optional[str]) -> None:
        self._set_color_property("fill_color", color)

    @property
    def border_color(self) -> str:
        """Return a valid border color, falling back without mutating stored data."""
        value = self.properties.get("border_color")
        return value if isinstance(value, str) and _HEX_COLOR_RE.fullmatch(value) else DEFAULT_BORDER_COLOR

    def set_border_color(self, color: Optional[str]) -> None:
        self._set_color_property("border_color", color)

    def _set_color_property(self, key: str, color: Optional[str]) -> None:
        if color is None or color == "":
            self.properties.pop(key, None)
            return
        if not isinstance(color, str) or not _HEX_COLOR_RE.fullmatch(color):
            raise ValueError(f"invalid visual screen color for {key}: {color!r}")
        self.properties[key] = color.upper()

    @property
    def icon(self) -> Optional[str]:
        """Return a supported stable icon ID, or ``None`` if unset/unknown."""
        value = self.properties.get("icon")
        return value if isinstance(value, str) and value in ICON_GLYPHS else None

    @property
    def icon_glyph(self) -> Optional[str]:
        """Return the render glyph for a supported icon ID."""
        icon_id = self.icon
        return ICON_GLYPHS.get(icon_id) if icon_id else None

    def set_icon(self, icon_id: Optional[str]) -> None:
        if icon_id is None or icon_id == "":
            self.properties.pop("icon", None)
            return
        if not isinstance(icon_id, str) or icon_id not in ICON_GLYPHS:
            raise ValueError(f"invalid visual screen icon: {icon_id!r}")
        self.properties["icon"] = icon_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VisualScreen":
        screen_id = str(data.get("id") or "").strip()
        name = str(data.get("name", "Untitled screen")).strip() or "Untitled screen"
        properties = data.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        return cls(
            id=screen_id,
            name=name,
            x=float(data.get("x", 60)),
            y=float(data.get("y", 60)),
            width=max(40.0, float(data.get("width", 180))),
            height=max(32.0, float(data.get("height", 88))),
            properties=dict(properties),
        )


@dataclass(frozen=True)
class VisualConnection:
    source_id: str
    destination_id: str
    label: Optional[str] = None
    condition: Optional[str] = None
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.label is not None and not isinstance(self.label, str):
            raise ValueError("visual connection label must be a string")
        if self.label == "":
            object.__setattr__(self, "label", None)
        if self.condition is not None and not isinstance(self.condition, str):
            raise ValueError("visual connection condition must be a string")
        if self.condition is not None and not self.condition.strip():
            object.__setattr__(self, "condition", None)
        object.__setattr__(self, "extra_fields", dict(self.extra_fields or {}))

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.extra_fields)
        data.update({"from": self.source_id, "to": self.destination_id})
        if self.label:
            data["label"] = self.label
        if self.condition:
            data["condition"] = self.condition
        return data


class VisualDesignDocument:
    """Mutable screen/connection document with JSON-compatible serialization."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        screens: Optional[list[VisualScreen]] = None,
        connections: Optional[list[VisualConnection]] = None,
        entry_screen_id: Optional[str] = None,
    ) -> None:
        self.screens = list(screens or [])
        self.connections = list(connections or [])
        self.entry_screen_id = entry_screen_id
        self._validate()

    @classmethod
    def empty(cls) -> "VisualDesignDocument":
        return cls()

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "VisualDesignDocument":
        if not isinstance(data, dict):
            return cls.empty()
        raw_screens = data.get("screens", [])
        raw_connections = data.get("connections", [])
        if not isinstance(raw_screens, list) or not isinstance(raw_connections, list):
            raise ValueError("visual_design screens and connections must be lists")

        screens = [
            VisualScreen.from_dict(item)
            for item in raw_screens
            if isinstance(item, dict)
        ]
        connections: list[VisualConnection] = []
        for item in raw_connections:
            if not isinstance(item, dict):
                continue
            source_id = item.get("from", item.get("source_id"))
            destination_id = item.get("to", item.get("destination_id"))
            raw_label = item.get("label")
            label = raw_label if isinstance(raw_label, str) and raw_label else None
            raw_condition = item.get("condition")
            condition = (
                raw_condition
                if isinstance(raw_condition, str) and raw_condition.strip()
                else None
            )
            extra_fields = {
                key: value
                for key, value in item.items()
                if key not in {
                    "from", "to", "source_id", "destination_id", "label", "condition"
                }
            }
            connections.append(
                VisualConnection(
                    str(source_id or ""),
                    str(destination_id or ""),
                    label=label,
                    condition=condition,
                    extra_fields=extra_fields,
                )
            )
        raw_entry_id = data.get("entry_screen_id")
        screen_ids = {screen.id for screen in screens}
        entry_screen_id = (
            raw_entry_id
            if isinstance(raw_entry_id, str) and raw_entry_id in screen_ids
            else None
        )
        return cls(
            screens=screens,
            connections=connections,
            entry_screen_id=entry_screen_id,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": self.SCHEMA_VERSION,
            "screens": [screen.to_dict() for screen in self.screens],
            "connections": [connection.to_dict() for connection in self.connections],
        }
        if self.entry_screen_id is not None:
            data["entry_screen_id"] = self.entry_screen_id
        return data

    def create_screen(
        self,
        name: str = "New screen",
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
        width: float = 180,
        height: float = 88,
    ) -> VisualScreen:
        index = len(self.screens) % 8
        screen = VisualScreen(
            id=f"screen-{uuid.uuid4().hex}",
            name=str(name).strip() or "Untitled screen",
            x=float(x if x is not None else 60 + index * 36),
            y=float(y if y is not None else 60 + index * 36),
            width=max(40.0, float(width)),
            height=max(32.0, float(height)),
            properties={
                "screen_type": DEFAULT_SCREEN_TYPE,
                "shape": DEFAULT_SHAPE,
                "fill_color": DEFAULT_FILL_COLOR,
                "border_color": DEFAULT_BORDER_COLOR,
                "description": "",
                "links": [],
            },
        )
        self.screens.append(screen)
        return screen

    def get_screen(self, screen_id: str) -> Optional[VisualScreen]:
        return next((screen for screen in self.screens if screen.id == screen_id), None)

    def set_entry_screen_id(self, screen_id: Optional[str]) -> None:
        if screen_id is None:
            self.entry_screen_id = None
            return
        if not isinstance(screen_id, str) or self.get_screen(screen_id) is None:
            raise ValueError(f"unknown entry screen id: {screen_id!r}")
        self.entry_screen_id = screen_id

    def rename_screen(self, screen_id: str, name: str) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.name = str(name).strip() or "Untitled screen"
        return screen

    def move_screen(self, screen_id: str, x: float, y: float) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.x = max(0.0, float(x))
        screen.y = max(0.0, float(y))
        return screen

    def set_screen_type(self, screen_id: str, screen_type: str) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_screen_type(screen_type)
        return screen

    def set_description(self, screen_id: str, description: str) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_description(description)
        return screen

    def set_links(self, screen_id: str, links: list[dict[str, Any]]) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_links(links)
        return screen

    @staticmethod
    def validate_links(links: Any) -> list[dict[str, Any]]:
        """Validate candidate link records before the GUI stages them."""
        return _validate_links(links)

    def set_shape(self, screen_id: str, shape: str) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_shape(shape)
        return screen

    def set_fill_color(self, screen_id: str, color: Optional[str]) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_fill_color(color)
        return screen

    def set_border_color(self, screen_id: str, color: Optional[str]) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_border_color(color)
        return screen

    def set_icon(self, screen_id: str, icon_id: Optional[str]) -> VisualScreen:
        screen = self._require_screen(screen_id)
        screen.set_icon(icon_id)
        return screen

    def delete_screen(self, screen_id: str) -> bool:
        if self.get_screen(screen_id) is None:
            return False
        if self.entry_screen_id == screen_id:
            self.entry_screen_id = None
        self.screens = [screen for screen in self.screens if screen.id != screen_id]
        self.connections = [
            connection
            for connection in self.connections
            if connection.source_id != screen_id
            and connection.destination_id != screen_id
        ]
        return True

    def connect(
        self,
        source_id: str,
        destination_id: str,
        label: Optional[str] = None,
        condition: Optional[str] = None,
    ) -> VisualConnection:
        if label is not None and not isinstance(label, str):
            raise ValueError("visual connection label must be a string")
        if condition is not None and not isinstance(condition, str):
            raise ValueError("visual connection condition must be a string")
        self._require_screen(source_id)
        self._require_screen(destination_id)
        if source_id == destination_id:
            raise ValueError("a screen cannot connect to itself")
        for connection in self.connections:
            if (
                connection.source_id == source_id
                and connection.destination_id == destination_id
            ):
                return connection
        connection = VisualConnection(
            source_id, destination_id, label=label, condition=condition
        )
        self.connections.append(connection)
        return connection

    def set_connection_label(
        self,
        source_id: str,
        destination_id: str,
        label: Optional[str],
    ) -> VisualConnection:
        if label is not None and not isinstance(label, str):
            raise ValueError("visual connection label must be a string")
        for index, connection in enumerate(self.connections):
            if (
                connection.source_id == source_id
                and connection.destination_id == destination_id
            ):
                updated = VisualConnection(
                    source_id,
                    destination_id,
                    label=label,
                    condition=connection.condition,
                    extra_fields=connection.extra_fields,
                )
                self.connections[index] = updated
                return updated
        raise ValueError(
            f"unknown visual connection: {source_id!r} -> {destination_id!r}"
        )

    def set_connection_condition(
        self,
        source_id: str,
        destination_id: str,
        condition: Optional[str],
    ) -> VisualConnection:
        if condition is not None and not isinstance(condition, str):
            raise ValueError("visual connection condition must be a string")
        for index, connection in enumerate(self.connections):
            if (
                connection.source_id == source_id
                and connection.destination_id == destination_id
            ):
                updated = VisualConnection(
                    source_id,
                    destination_id,
                    label=connection.label,
                    condition=condition,
                    extra_fields=connection.extra_fields,
                )
                self.connections[index] = updated
                return updated
        raise ValueError(
            f"unknown visual connection: {source_id!r} -> {destination_id!r}"
        )

    def disconnect(self, source_id: str, destination_id: str) -> bool:
        before = len(self.connections)
        self.connections = [
            connection
            for connection in self.connections
            if not (
                connection.source_id == source_id
                and connection.destination_id == destination_id
            )
        ]
        return len(self.connections) != before

    def _require_screen(self, screen_id: str) -> VisualScreen:
        screen = self.get_screen(screen_id)
        if screen is None:
            raise ValueError(f"unknown screen id: {screen_id!r}")
        return screen

    def _validate(self) -> None:
        screen_ids = [screen.id for screen in self.screens]
        if any(not screen_id for screen_id in screen_ids):
            raise ValueError("every visual screen must have a non-empty id")
        if len(screen_ids) != len(set(screen_ids)):
            raise ValueError("visual screen ids must be unique")
        if self.entry_screen_id is not None and (
            not isinstance(self.entry_screen_id, str)
            or self.entry_screen_id not in set(screen_ids)
        ):
            raise ValueError(
                f"entry_screen_id must reference an existing screen: {self.entry_screen_id!r}"
            )
        known_ids = set(screen_ids)
        pairs: set[tuple[str, str]] = set()
        for connection in self.connections:
            pair = (connection.source_id, connection.destination_id)
            if not all(pair) or pair[0] not in known_ids or pair[1] not in known_ids:
                raise ValueError("visual connections must reference existing screen ids")
            if pair[0] == pair[1]:
                raise ValueError("a screen cannot connect to itself")
            if pair in pairs:
                raise ValueError("visual connections must be unique")
            pairs.add(pair)


__all__ = [
    "DEFAULT_SCREEN_TYPE",
    "DEFAULT_SHAPE",
    "DEFAULT_FILL_COLOR",
    "DEFAULT_BORDER_COLOR",
    "ICON_GLYPHS",
    "ICON_OPTIONS",
    "SCREEN_TYPES",
    "SHAPES",
    "SHAPE_OPTIONS",
    "VisualConnection",
    "VisualDesignDocument",
    "VisualScreen",
]
