import pytest
from tests.conftest import gisai_available


def test_import_giscode():
    import giscode
    assert hasattr(giscode, "__version__")


def test_config_manager_load_save(tmp_path):
    from giscode.config.manager import ConfigManager

    config_path = tmp_path / "config.json"
    manager = ConfigManager(config_path)
    manager.save({"key": "value"})
    data = manager.load()
    assert data["key"] == "value"


def test_settings_manager(tmp_path):
    from giscode.config.settings import SettingsManager

    config_path = tmp_path / "config.json"
    manager = SettingsManager(config_path)
    manager.set("app.name", "GisCode")
    assert manager.get("app.name") == "GisCode"
    assert manager.get("app.missing", "default") == "default"


def test_workspace_creation(tmp_path):
    from giscode.workspace.workspace import Workspace

    ws = Workspace(tmp_path / "project")
    assert ws.root_path.exists()
    assert (ws.root_path / "src").exists()
    assert (ws.root_path / "tests").exists()


def test_workspace_read_write(tmp_path):
    from giscode.workspace.workspace import Workspace

    ws = Workspace(tmp_path / "project")
    target = ws.write("src", "main.py", content="print('hello')")
    assert target.exists()
    assert ws.read("src", "main.py") == "print('hello')"


def test_logger_setup():
    from giscode.logging.logger import get_logger, setup_logger

    logger = setup_logger(name="test_foundation")
    assert logger.name == "test_foundation"
    assert logger.level == 20  # INFO


def test_inference_engine_is_abstract():
    from giscode.inference.engine import InferenceEngine

    with pytest.raises(TypeError):
        InferenceEngine()


def test_agent_loop_is_abstract():
    from giscode.agent.loop import AgentLoop

    with pytest.raises(TypeError):
        AgentLoop()


def test_tool_executor_is_abstract():
    from giscode.tools.executor import ToolExecutor

    with pytest.raises(TypeError):
        ToolExecutor()


def test_verifier_is_abstract():
    from giscode.verification.verifier import Verifier

    with pytest.raises(TypeError):
        Verifier()


def test_auditor_is_abstract():
    from giscode.audit.auditor import Auditor

    with pytest.raises(TypeError):
        Auditor()


def test_knowledge_store_is_abstract():
    from giscode.knowledge.store import KnowledgeStore

    with pytest.raises(TypeError):
        KnowledgeStore()


def test_gui_launches(tk_root):
    from giscode.gui.main_window import MainWindow

    window = MainWindow(tk_root)
    assert window.phase_var.get() == "Idle"


def test_main_bootstrap():
    from giscode.main import bootstrap

    settings, workspace, task_manager, inference, model_manager, coding_agent, terminal_executor, sandbox_executor, orchestrator, openhands_adapter, knowledge_service, routing_service, ocr_service, monitoring_service, packaging_builder, signing_service, self_improvement_service, research_service, gisai_engine = bootstrap()
    assert settings is not None
    assert workspace is not None
    assert task_manager is not None
    assert inference is not None
    assert model_manager is not None
    assert coding_agent is not None
    assert terminal_executor is not None
    assert sandbox_executor is not None
    assert orchestrator is not None
    assert openhands_adapter is not None
    assert knowledge_service is not None
    assert routing_service is not None
    assert ocr_service is not None
    assert monitoring_service is not None
    assert packaging_builder is not None
    assert signing_service is not None
    assert self_improvement_service is not None
    assert research_service is not None
    if gisai_available():
        assert gisai_engine is not None, "a GisAI build must bootstrap its engine"
    else:
        assert gisai_engine is None, (
            "a Core build without GisAI must leave slot 18 empty rather than "
            "half-initialised"
        )
