import json

from giscode.projects.context import ProjectContext, ProjectContextStore


def test_create_sets_identity_from_root_path(tmp_path):
    root = tmp_path / "MyApp"
    root.mkdir()
    ctx = ProjectContext.create(str(root))

    assert ctx.name == "MyApp"
    assert ctx.root_path == str(root.resolve())
    assert ctx.active_section == "Overview"
    assert ctx.project_id  # stable non-empty id


def test_enter_section_keeps_history_and_dedupes(tmp_path):
    root = tmp_path
    ctx = ProjectContext.create(str(root), section="Overview")

    ctx.enter_section("Backend")
    assert ctx.active_section == "Backend"
    assert ctx.section_history == ["Overview"]

    ctx.enter_section("Backend")
    assert ctx.section_history == ["Overview"]

    ctx.enter_section("Testing")
    assert ctx.section_history == ["Backend", "Overview"]

    assert ctx.back_to_previous_section() == "Backend"
    assert ctx.active_section == "Backend"
    assert ctx.section_history == ["Overview"]

    assert ctx.back_to_previous_section() == "Overview"
    assert ctx.section_history == []

    assert ctx.back_to_previous_section() == "Overview"


def test_section_history_is_capped():
    ctx = ProjectContext(project_id="p", name="p", root_path="C:/p", active_section="Overview")
    for i in range(20):
        ctx.enter_section(f"Section {i}")
    assert len(ctx.section_history) <= 10


def test_store_round_trip(tmp_path):
    store = ProjectContextStore(tmp_path)
    root = tmp_path / "ProjectX"
    root.mkdir()
    ctx = ProjectContext.create(str(root))
    ctx.enter_section("Frontend")
    ctx.task_status = "completed"
    ctx.current_task_id = "task-1"
    store.save(ctx)

    loaded = store.load_all()
    assert ctx.project_id in loaded
    restored = loaded[ctx.project_id]
    assert restored.name == "ProjectX"
    assert restored.root_path == ctx.root_path
    assert restored.active_section == "Frontend"
    assert restored.section_history == ["Overview"]
    assert restored.task_status == "completed"
    assert restored.current_task_id == "task-1"


def test_store_ignores_corrupt_files(tmp_path):
    store = ProjectContextStore(tmp_path)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    assert store.load_all() == {}


def test_from_dict_ignores_unknown_keys():
    ctx = ProjectContext.from_dict(
        {"project_id": "p", "name": "X", "root_path": "C:/x", "bogus": 1}
    )
    assert ctx.project_id == "p"
    assert ctx.name == "X"