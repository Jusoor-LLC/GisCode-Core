"""Data model, project persistence, and isolation tests for Visual Design."""
from __future__ import annotations

import json

import pytest

from giscode.gui.project_workspace import ProjectWorkspaceController
from giscode.projects import ProjectStore, VisualDesignDocument
from giscode.storage import ChatStore


def test_visual_design_model_screen_lifecycle_and_connections():
    document = VisualDesignDocument.empty()
    home = document.create_screen("Home", x=24, y=42)
    details = document.create_screen("Details", x=360, y=200)

    assert home.id != details.id
    stable_id = home.id
    document.rename_screen(home.id, "Welcome")
    document.move_screen(home.id, 120, 90)
    connection = document.connect(home.id, details.id)

    assert home.id == stable_id
    assert home.name == "Welcome"
    assert (home.x, home.y) == (120, 90)
    assert connection.to_dict() == {"from": home.id, "to": details.id}
    assert json.loads(json.dumps(document.to_dict()))["connections"] == [
        {"from": home.id, "to": details.id}
    ]

    restored = VisualDesignDocument.from_dict(document.to_dict())
    assert restored.to_dict() == document.to_dict()
    assert restored.get_screen(home.id).x == 120

    assert restored.delete_screen(home.id) is True
    assert restored.get_screen(home.id) is None
    assert restored.connections == []
    assert restored.delete_screen(home.id) is False


def test_visual_design_model_rejects_invalid_relationship_ids():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("Only screen")

    try:
        document.connect(screen.id, "missing-screen")
    except ValueError as exc:
        assert "unknown screen id" in str(exc)
    else:
        raise AssertionError("connection to missing screen was accepted")

    try:
        VisualDesignDocument.from_dict(
            {
                "screens": [{"id": screen.id, "name": "Only screen"}],
                "connections": [{"from": screen.id, "to": "missing-screen"}],
            }
        )
    except ValueError as exc:
        assert "existing screen ids" in str(exc)
    else:
        raise AssertionError("invalid persisted connection was accepted")


def test_visual_design_entry_screen_validation_rename_clear_and_delete():
    document = VisualDesignDocument.empty()
    welcome = document.create_screen("Welcome")
    duplicate_name = document.create_screen("Welcome")

    assert document.entry_screen_id is None
    assert "entry_screen_id" not in document.to_dict()
    assert "entry_screen_id" not in welcome.properties

    document.set_entry_screen_id(welcome.id)
    assert document.entry_screen_id == welcome.id
    assert document.to_dict()["entry_screen_id"] == welcome.id
    serialized = json.loads(json.dumps(document.to_dict()))
    assert VisualDesignDocument.from_dict(serialized).entry_screen_id == welcome.id
    document.rename_screen(welcome.id, "Renamed start")
    assert document.entry_screen_id == welcome.id

    # There is one project-level ID, so designating another screen replaces it.
    document.set_entry_screen_id(duplicate_name.id)
    assert document.entry_screen_id == duplicate_name.id
    with pytest.raises(ValueError, match="unknown entry screen id"):
        document.set_entry_screen_id("missing-screen")
    assert document.entry_screen_id == duplicate_name.id

    document.set_entry_screen_id(None)
    assert document.entry_screen_id is None
    assert "entry_screen_id" not in document.to_dict()
    document.set_entry_screen_id(duplicate_name.id)
    assert document.delete_screen(duplicate_name.id) is True
    assert document.entry_screen_id is None
    assert document.get_screen(welcome.id) is not None

    legacy_data = {
        "screens": [{"id": "old", "name": "Welcome", "properties": {}}],
        "connections": [],
    }
    legacy = VisualDesignDocument.from_dict(legacy_data)
    assert legacy.entry_screen_id is None
    assert "entry_screen_id" not in legacy.to_dict()
    assert "entry_screen_id" not in legacy_data

    invalid_entry = VisualDesignDocument.from_dict(
        {
            "screens": [{"id": "old", "name": "Welcome", "properties": {}}],
            "connections": [],
            "entry_screen_id": "not-present",
        }
    )
    assert invalid_entry.entry_screen_id is None
    assert len(invalid_entry.screens) == 1
    assert "entry_screen_id" not in invalid_entry.to_dict()
    with pytest.raises(ValueError, match="entry_screen_id must reference"):
        VisualDesignDocument(entry_screen_id="missing")


def test_visual_connection_labels_are_optional_editable_and_preserved():
    document = VisualDesignDocument.empty()
    login = document.create_screen("Login")
    dashboard = document.create_screen("Dashboard")
    details = document.create_screen("Details")

    unlabeled = document.connect(login.id, dashboard.id)
    assert unlabeled.label is None
    assert unlabeled.condition is None
    assert unlabeled.to_dict() == {"from": login.id, "to": dashboard.id}

    labeled = document.connect(dashboard.id, details.id, label="Select a project")
    assert labeled.label == "Select a project"
    document.set_connection_label(login.id, dashboard.id, "Tap Sign in")
    document.set_connection_label(dashboard.id, details.id, "Open project details")
    document.set_connection_condition(
        login.id, dashboard.id, "When authentication succeeds"
    )
    document.connect(
        details.id, login.id, condition="If the user chooses to sign in again"
    )
    assert [
        (connection.label, connection.condition)
        for connection in document.connections
    ] == [
        ("Tap Sign in", "When authentication succeeds"),
        ("Open project details", None),
        (None, "If the user chooses to sign in again"),
    ]
    assert document.connections[0].to_dict() == {
        "from": login.id,
        "to": dashboard.id,
        "label": "Tap Sign in",
        "condition": "When authentication succeeds",
    }

    with pytest.raises(ValueError, match="label must be a string"):
        document.set_connection_label(login.id, dashboard.id, 10)
    with pytest.raises(ValueError, match="condition must be a string"):
        document.set_connection_condition(login.id, dashboard.id, 10)
    assert document.connections[0].label == "Tap Sign in"
    assert document.connections[0].condition == "When authentication succeeds"

    document.set_connection_label(login.id, dashboard.id, "")
    assert document.connections[0].label is None
    document.set_connection_condition(dashboard.id, details.id, " \n ")
    assert document.connections[1].condition is None
    assert document.connections[0].to_dict() == {
        "from": login.id,
        "to": dashboard.id,
        "condition": "When authentication succeeds",
    }

    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {"id": "from", "name": "From", "properties": {"keep": True}},
                {"id": "to", "name": "To", "properties": {"also_keep": 2}},
            ],
            "connections": [{"from": "from", "to": "to"}],
        }
    )
    assert legacy.connections[0].label is None
    assert legacy.to_dict()["connections"] == [{"from": "from", "to": "to"}]
    legacy.set_connection_label("from", "to", "Tap Continue")
    assert legacy.to_dict()["connections"] == [
        {"from": "from", "to": "to", "label": "Tap Continue"}
    ]
    legacy.set_connection_condition("from", "to", "When the user continues")
    assert legacy.to_dict()["connections"] == [
        {
            "from": "from",
            "to": "to",
            "label": "Tap Continue",
            "condition": "When the user continues",
        }
    ]
    assert legacy.screens[0].properties == {"keep": True}
    assert legacy.screens[1].properties == {"also_keep": 2}

    extended = VisualDesignDocument.from_dict(
        {
            "screens": [
                {"id": "from", "name": "From"},
                {"id": "to", "name": "To"},
            ],
            "connections": [
                {
                    "from": "from",
                    "to": "to",
                    "label": "Existing action",
                    "extension": {"retain": True},
                }
            ],
        }
    )
    extended.set_connection_label("from", "to", "Updated action")
    extended.set_connection_condition("from", "to", "When ready")
    assert extended.to_dict()["connections"] == [
        {
            "from": "from",
            "to": "to",
            "label": "Updated action",
            "condition": "When ready",
            "extension": {"retain": True},
        }
    ]

    malformed_fields = VisualDesignDocument.from_dict(
        {
            "screens": [
                {"id": "from", "name": "From"},
                {"id": "to", "name": "To"},
                {"id": "other", "name": "Other"},
            ],
            "connections": [
                {"from": "from", "to": "to", "label": 5},
                {
                    "from": "to",
                    "to": "other",
                    "label": "Valid",
                    "condition": "When the user confirms",
                },
                {
                    "from": "other",
                    "to": "from",
                    "condition": {"not": "text"},
                    "unrelated": "preserved",
                },
            ],
        }
    )
    assert len(malformed_fields.connections) == 3
    assert malformed_fields.connections[0].label is None
    assert malformed_fields.connections[0].condition is None
    assert malformed_fields.connections[1].label == "Valid"
    assert malformed_fields.connections[1].condition == "When the user confirms"
    assert malformed_fields.connections[2].condition is None
    assert malformed_fields.connections[2].to_dict() == {
        "from": "other",
        "to": "from",
        "unrelated": "preserved",
    }


def test_screen_type_default_change_and_validation():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("Home")

    assert screen.screen_type == "Generic"
    assert screen.properties["screen_type"] == "Generic"
    document.set_screen_type(screen.id, "Dashboard")
    assert screen.screen_type == "Dashboard"
    assert screen.properties["screen_type"] == "Dashboard"

    with pytest.raises(ValueError, match="invalid screen type"):
        document.set_screen_type(screen.id, "Unknown type")
    assert screen.screen_type == "Dashboard"

    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "legacy-screen",
                    "name": "Legacy",
                    "properties": {"custom": "preserve"},
                }
            ],
            "connections": [],
        }
    ).get_screen("legacy-screen")
    assert legacy.screen_type == "Generic"
    assert legacy.properties == {"custom": "preserve"}

    unsupported_legacy_type = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "future-screen",
                    "name": "Future",
                    "properties": {
                        "screen_type": "Future type",
                        "custom": "keep",
                    },
                }
            ],
            "connections": [],
        }
    ).get_screen("future-screen")
    assert unsupported_legacy_type.screen_type == "Generic"
    assert unsupported_legacy_type.properties == {
        "screen_type": "Future type",
        "custom": "keep",
    }


def test_screen_description_serialization_and_legacy_default():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("Welcome")
    assert screen.description == ""
    assert screen.properties["description"] == ""

    description = "Show a greeting.\nInclude the account status and next action."
    document.set_description(screen.id, description)
    screen.properties["other_property"] = {"retain": True}
    assert screen.description == description
    assert screen.properties["description"] == description

    serialized = json.loads(json.dumps(document.to_dict()))
    restored = VisualDesignDocument.from_dict(serialized)
    restored_screen = restored.get_screen(screen.id)
    assert restored_screen.description == description
    assert restored_screen.properties["other_property"] == {"retain": True}

    document.set_description(screen.id, "")
    assert screen.description == ""
    assert screen.properties["description"] == ""
    with pytest.raises(ValueError, match="must be a string"):
        document.set_description(screen.id, None)

    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "legacy-description-screen",
                    "name": "Legacy",
                    "properties": {"screen_type": "Login", "custom": "keep"},
                }
            ],
            "connections": [],
        }
    ).get_screen("legacy-description-screen")
    assert legacy.description == ""
    assert legacy.properties == {"screen_type": "Login", "custom": "keep"}


def test_screen_links_validate_edit_remove_and_roundtrip():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("References")
    assert screen.links == []
    assert screen.properties["links"] == []

    records = [
        {"label": "Requirements", "url": "https://example.test/requirements"},
        {"label": "", "url": "C:/project/docs/api.md"},
    ]
    document.set_links(screen.id, records)
    assert screen.links == records
    assert screen.properties["links"] == records

    edited = [
        {"label": "Updated requirements", "url": "https://example.test/revised"},
        records[1],
    ]
    document.set_links(screen.id, edited)
    assert screen.links == edited
    document.set_links(screen.id, [edited[0]])
    assert screen.links == [edited[0]]

    serialized = json.loads(json.dumps(document.to_dict()))
    restored = VisualDesignDocument.from_dict(serialized).get_screen(screen.id)
    assert restored.links == [edited[0]]

    previous_links = screen.links
    for invalid in (
        {"label": 5, "url": "https://example.test"},
        {"label": "No URL", "url": ""},
        {"label": "No URL", "url": "  "},
        "not a record",
    ):
        with pytest.raises(ValueError):
            document.set_links(screen.id, [invalid])
        assert screen.links == previous_links

    document.set_links(screen.id, [])
    assert screen.links == []
    assert screen.properties["links"] == []


def test_legacy_and_malformed_screen_links_are_read_safely():
    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "legacy-links",
                    "name": "Legacy",
                    "properties": {"custom": "preserve"},
                }
            ],
            "connections": [],
        }
    ).get_screen("legacy-links")
    assert legacy.links == []
    assert "links" not in legacy.properties

    raw_links = [
        {"label": "Good", "url": "https://example.test/good"},
        {"label": "Missing URL"},
        {"label": None, "url": "https://example.test/bad"},
        {"label": "Empty URL", "url": ""},
        17,
    ]
    malformed = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "malformed-links",
                    "name": "Malformed",
                    "properties": {"links": raw_links, "custom": "keep"},
                }
            ],
            "connections": [],
        }
    ).get_screen("malformed-links")
    assert malformed.links == [raw_links[0]]
    assert malformed.properties["links"] == raw_links
    assert malformed.properties["custom"] == "keep"


def test_shape_default_change_validation_and_legacy_compatibility():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("Home")

    assert screen.shape == "rounded"
    assert screen.properties["shape"] == "rounded"
    document.set_shape(screen.id, "diamond")
    assert screen.shape == "diamond"
    assert screen.properties["shape"] == "diamond"
    assert json.loads(json.dumps(screen.to_dict()))["properties"]["shape"] == "diamond"

    with pytest.raises(ValueError, match="invalid visual screen shape"):
        document.set_shape(screen.id, "triangle")
    with pytest.raises(ValueError, match="invalid visual screen shape"):
        document.set_shape(screen.id, ["not", "a", "shape"])
    assert screen.shape == "diamond"

    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "legacy-shape-screen",
                    "name": "Legacy",
                    "properties": {"screen_type": "Login", "custom": "keep"},
                }
            ],
            "connections": [],
        }
    ).get_screen("legacy-shape-screen")
    assert legacy.shape == "rounded"
    assert legacy.properties == {"screen_type": "Login", "custom": "keep"}

    unsupported = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "unsupported-shape-screen",
                    "name": "Unsupported",
                    "properties": {"shape": "triangle", "custom": "preserve"},
                }
            ],
            "connections": [],
        }
    ).get_screen("unsupported-shape-screen")
    assert unsupported.shape == "rounded"
    assert unsupported.properties == {"shape": "triangle", "custom": "preserve"}


def test_screen_colors_default_validation_reset_and_legacy_compatibility():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("Home")

    assert screen.fill_color == "#FFFFFF"
    assert screen.border_color == "#DAD8CE"
    assert screen.properties["fill_color"] == "#FFFFFF"
    assert screen.properties["border_color"] == "#DAD8CE"

    document.set_fill_color(screen.id, "#e7ecfb")
    document.set_border_color(screen.id, "#3d4eba")
    assert screen.fill_color == "#E7ECFB"
    assert screen.border_color == "#3D4EBA"
    assert screen.properties["fill_color"] == "#E7ECFB"
    assert screen.properties["border_color"] == "#3D4EBA"
    encoded = json.loads(json.dumps(screen.to_dict()))["properties"]
    assert encoded["fill_color"] == "#E7ECFB"
    assert encoded["border_color"] == "#3D4EBA"

    with pytest.raises(ValueError, match="invalid visual screen color"):
        document.set_fill_color(screen.id, "red")
    with pytest.raises(ValueError, match="invalid visual screen color"):
        document.set_border_color(screen.id, "#12345")
    assert screen.fill_color == "#E7ECFB"
    assert screen.border_color == "#3D4EBA"

    document.set_fill_color(screen.id, None)
    document.set_border_color(screen.id, "")
    assert screen.fill_color == "#FFFFFF"
    assert screen.border_color == "#DAD8CE"
    assert "fill_color" not in screen.properties
    assert "border_color" not in screen.properties

    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "legacy-color-screen",
                    "name": "Legacy",
                    "properties": {"screen_type": "Login", "custom": "keep"},
                }
            ],
            "connections": [],
        }
    ).get_screen("legacy-color-screen")
    assert legacy.fill_color == "#FFFFFF"
    assert legacy.border_color == "#DAD8CE"
    assert legacy.properties == {"screen_type": "Login", "custom": "keep"}

    invalid_legacy_colors = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "invalid-color-screen",
                    "name": "Invalid colors",
                    "properties": {
                        "fill_color": "not-a-color",
                        "border_color": "#12345",
                        "custom": "preserve",
                    },
                }
            ],
            "connections": [],
        }
    ).get_screen("invalid-color-screen")
    assert invalid_legacy_colors.fill_color == "#FFFFFF"
    assert invalid_legacy_colors.border_color == "#DAD8CE"
    assert invalid_legacy_colors.properties == {
        "fill_color": "not-a-color",
        "border_color": "#12345",
        "custom": "preserve",
    }


def test_icon_assignment_clear_and_invalid_values():
    document = VisualDesignDocument.empty()
    screen = document.create_screen("Home")

    assert screen.icon is None
    assert "icon" not in screen.properties
    document.set_icon(screen.id, "home")
    assert screen.icon == "home"
    assert screen.icon_glyph == "🏠"
    assert screen.properties["icon"] == "home"
    assert json.loads(json.dumps(screen.to_dict()))["properties"]["icon"] == "home"

    with pytest.raises(ValueError, match="invalid visual screen icon"):
        document.set_icon(screen.id, "unlisted-icon")
    with pytest.raises(ValueError, match="invalid visual screen icon"):
        document.set_icon(screen.id, ["not", "a", "key"])
    assert screen.icon == "home"

    document.set_icon(screen.id, None)
    assert screen.icon is None
    assert "icon" not in screen.properties

    legacy = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "legacy-icon-screen",
                    "name": "Legacy",
                    "properties": {"screen_type": "Login", "custom": "keep"},
                }
            ],
            "connections": [],
        }
    ).get_screen("legacy-icon-screen")
    assert legacy.icon is None
    assert legacy.icon_glyph is None
    assert legacy.properties == {"screen_type": "Login", "custom": "keep"}

    unsupported = VisualDesignDocument.from_dict(
        {
            "screens": [
                {
                    "id": "unsupported-icon-screen",
                    "name": "Unsupported",
                    "properties": {"icon": "legacy-glyph", "custom": "keep"},
                }
            ],
            "connections": [],
        }
    ).get_screen("unsupported-icon-screen")
    assert unsupported.icon is None
    assert unsupported.icon_glyph is None
    assert unsupported.properties == {"icon": "legacy-glyph", "custom": "keep"}


def test_visual_design_roundtrip_preserves_planner_mode_and_metadata(tmp_path):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    metadata = {"origin": "test-fixture", "flags": ["keep"]}
    try:
        session = controller.create_project(
            "Visual project",
            str(tmp_path / "visual-project"),
            plan_data={"custom_metadata": metadata},
            mode="visual_app_design",
        )
        session.engine.start_with_idea("A project with a visual flow")
        controller.save_plan()

        design = VisualDesignDocument.empty()
        welcome = design.create_screen("Welcome", x=32, y=48)
        main = design.create_screen("Main", x=360, y=180)
        details = design.create_screen("Details", x=640, y=320)
        confirmation = design.create_screen("Confirmation", x=900, y=400)
        design.set_entry_screen_id(welcome.id)
        welcome.properties["unrelated_visual_property"] = {"keep": True}
        design.set_screen_type(welcome.id, "Dashboard")
        design.set_icon(welcome.id, "launch")
        design.set_shape(welcome.id, "diamond")
        design.set_fill_color(welcome.id, "#E7ECFB")
        design.set_border_color(welcome.id, "#3D4EBA")
        description = "Welcome the user.\nOffer sign-in and guest access."
        design.set_description(welcome.id, description)
        links = [
            {"label": "Requirements", "url": "https://example.test/requirements"},
            {"label": "Local API docs", "url": "docs/api.md"},
        ]
        design.set_links(welcome.id, links)
        design.connect(
            welcome.id,
            main.id,
            label="Tap Sign in",
            condition="When authentication succeeds",
        )
        design.connect(main.id, details.id, label="Choose a project")
        design.connect(welcome.id, details.id)
        design.connect(
            details.id,
            confirmation.id,
            condition="When the project is ready",
        )
        controller.save_visual_design(design.to_dict())
        project_id = session.project_id

        controller.close()
        reopened = controller.open_project(project_id)
        stored_design = VisualDesignDocument.from_dict(controller.get_visual_design())
        assert stored_design.to_dict() == design.to_dict()
        assert stored_design.entry_screen_id == welcome.id
        restored_welcome = stored_design.get_screen(welcome.id)
        assert restored_welcome.screen_type == "Dashboard"
        assert restored_welcome.icon == "launch"
        assert restored_welcome.shape == "diamond"
        assert restored_welcome.fill_color == "#E7ECFB"
        assert restored_welcome.border_color == "#3D4EBA"
        assert restored_welcome.description == description
        assert restored_welcome.links == links
        assert [connection.label for connection in stored_design.connections] == [
            "Tap Sign in", "Choose a project", None, None
        ]
        assert [connection.condition for connection in stored_design.connections] == [
            "When authentication succeeds", None, None, "When the project is ready"
        ]
        assert restored_welcome.properties["icon"] == "launch"
        assert restored_welcome.properties["shape"] == "diamond"
        assert restored_welcome.properties["fill_color"] == "#E7ECFB"
        assert restored_welcome.properties["border_color"] == "#3D4EBA"
        assert restored_welcome.properties["description"] == description
        assert restored_welcome.properties["links"] == links
        assert restored_welcome.properties["unrelated_visual_property"] == {"keep": True}
        assert reopened.engine.plan.understanding.original_idea == \
            "A project with a visual flow"
        assert reopened.project.mode == "visual_app_design"
        assert reopened.project.plan["custom_metadata"] == metadata

        # A later Planner save also retains the Visual Design document.
        controller.save_plan()
        persisted = project_store.get_project(project_id)
        assert persisted is not None
        assert persisted.plan["visual_design"] == design.to_dict()
        assert persisted.plan["custom_metadata"] == metadata
        assert persisted.mode == "visual_app_design"
        assert persisted.plan["plan"]["understanding"]["original_idea"] == \
            "A project with a visual flow"
    finally:
        controller.close()
        project_store.close()
        chat_store.close()


def test_visual_design_projects_are_isolated_when_switching(tmp_path):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    controller = ProjectWorkspaceController(project_store, chat_store)
    try:
        project_a = controller.create_project("A", str(tmp_path / "a"), mode="visual_app_design")
        a_id = project_a.project_id
        a_doc = VisualDesignDocument.empty()
        a_screen = a_doc.create_screen("A screen", x=100, y=125)
        a_doc.set_entry_screen_id(a_screen.id)
        controller.save_visual_design(a_doc.to_dict())

        project_b = controller.create_project("B", str(tmp_path / "b"), mode="visual_app_design")
        b_id = project_b.project_id
        assert controller.get_visual_design().get("screens", []) == []
        b_doc = VisualDesignDocument.empty()
        b_screen = b_doc.create_screen("B screen", x=420, y=245)
        b_doc.set_entry_screen_id(b_screen.id)
        controller.save_visual_design(b_doc.to_dict())

        reopened_a = controller.open_project(a_id)
        assert reopened_a.project_id == a_id
        assert VisualDesignDocument.from_dict(controller.get_visual_design()).to_dict() == \
            a_doc.to_dict()
        assert reopened_a.project.plan["visual_design"]["entry_screen_id"] == a_screen.id
        reopened_b = controller.open_project(b_id)
        assert reopened_b.project_id == b_id
        assert VisualDesignDocument.from_dict(controller.get_visual_design()).to_dict() == \
            b_doc.to_dict()
        assert reopened_b.project.plan["visual_design"]["entry_screen_id"] == b_screen.id
        assert project_store.get_project(a_id).plan["visual_design"] == a_doc.to_dict()
        assert project_store.get_project(b_id).plan["visual_design"] == b_doc.to_dict()
    finally:
        controller.close()
        project_store.close()
        chat_store.close()
