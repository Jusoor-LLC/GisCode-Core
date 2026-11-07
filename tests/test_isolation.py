"""Tests for GISCODE_HOME isolation behavior."""
import os
import sys
import tempfile
from pathlib import Path

# Ensure the package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from giscode.config.paths import giscode_home, profile_root, set_giscode_home
from giscode.knowledge.config import KnowledgeStoreConfig


def test_giscode_home_default():
    """With GISCODE_HOME unset, giscode_home() equals Path.home()/.giscode."""
    if "GISCODE_HOME" in os.environ:
        del os.environ["GISCODE_HOME"]
    expected = Path.home() / ".giscode"
    assert giscode_home() == expected, f"Expected {expected}, got {giscode_home()}"
    print("OK test_giscode_home_default")


def test_giscode_home_override():
    """With GISCODE_HOME set, giscode_home() returns the override."""
    with tempfile.TemporaryDirectory() as tmpdir:
        override = Path(tmpdir) / "custom_giscode"
        set_giscode_home(override)
        try:
            assert giscode_home() == override.resolve(), f"Expected {override.resolve()}, got {giscode_home()}"
            print("OK test_giscode_home_override")
        finally:
            if "GISCODE_HOME" in os.environ:
                del os.environ["GISCODE_HOME"]


def test_profile_root_default():
    """With GISCODE_HOME unset, profile_root() equals Path.home()."""
    if "GISCODE_HOME" in os.environ:
        del os.environ["GISCODE_HOME"]
    assert profile_root() == Path.home(), f"Expected {Path.home()}, got {profile_root()}"
    print("OK test_profile_root_default")


def test_profile_root_override():
    """With GISCODE_HOME set, profile_root() returns the parent of the override."""
    with tempfile.TemporaryDirectory() as tmpdir:
        override = Path(tmpdir) / "custom_giscode"
        set_giscode_home(override)
        try:
            expected = override.parent.resolve()
            assert profile_root() == expected, f"Expected {expected}, got {profile_root()}"
            print("OK test_profile_root_override")
        finally:
            if "GISCODE_HOME" in os.environ:
                del os.environ["GISCODE_HOME"]


def test_knowledge_config_default_path():
    """With GISCODE_HOME unset, knowledge.db resolves to ~/.giscode/knowledge.db."""
    if "GISCODE_HOME" in os.environ:
        del os.environ["GISCODE_HOME"]
    config = KnowledgeStoreConfig()
    expected = Path.home() / ".giscode" / "knowledge.db"
    assert config.storage_path == expected, f"Expected {expected}, got {config.storage_path}"
    print("OK test_knowledge_config_default_path")


def test_knowledge_config_override_path():
    """With GISCODE_HOME set, knowledge.db resolves to override directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        override = Path(tmpdir) / "custom_giscode"
        set_giscode_home(override)
        try:
            config = KnowledgeStoreConfig()
            expected = override.resolve() / "knowledge.db"
            assert config.storage_path == expected, f"Expected {expected}, got {config.storage_path}"
            # Verify it uses the override, not the default ~/.giscode
            default_path = Path.home() / ".giscode" / "knowledge.db"
            assert config.storage_path != default_path, f"Should use override, not default: {config.storage_path}"
            print("OK test_knowledge_config_override_path")
        finally:
            if "GISCODE_HOME" in os.environ:
                del os.environ["GISCODE_HOME"]


def test_knowledge_config_safety_check_default():
    """Safety check passes for paths under real home when no override."""
    if "GISCODE_HOME" in os.environ:
        del os.environ["GISCODE_HOME"]
    config = KnowledgeStoreConfig(storage_path=str(Path.home() / ".giscode" / "custom.db"))
    assert config.storage_path == Path.home() / ".giscode" / "custom.db"
    print("OK test_knowledge_config_safety_check_default")


def test_knowledge_config_safety_check_override():
    """Safety check passes for paths under override when GISCODE_HOME is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        override = Path(tmpdir) / "custom_giscode"
        set_giscode_home(override)
        try:
            config = KnowledgeStoreConfig(storage_path=str(override / "custom.db"))
            assert config.storage_path == override.resolve() / "custom.db"
            print("OK test_knowledge_config_safety_check_override")
        finally:
            if "GISCODE_HOME" in os.environ:
                del os.environ["GISCODE_HOME"]


def test_knowledge_config_safety_check_rejects_outside():
    """Safety check rejects paths outside the profile root."""
    with tempfile.TemporaryDirectory() as tmpdir:
        override = Path(tmpdir) / "custom_giscode"
        set_giscode_home(override)
        try:
            # Create a path outside the profile root (which is tmpdir)
            outside = Path(tmpdir).parent / "truly_outside" / "custom.db"
            try:
                KnowledgeStoreConfig(storage_path=str(outside))
                assert False, "Should have raised PathSafetyError"
            except Exception as e:
                assert "outside" in str(e).lower() or "PathSafetyError" in type(e).__name__
                print("OK test_knowledge_config_safety_check_rejects_outside")
        finally:
            if "GISCODE_HOME" in os.environ:
                del os.environ["GISCODE_HOME"]


if __name__ == "__main__":
    test_giscode_home_default()
    test_giscode_home_override()
    test_profile_root_default()
    test_profile_root_override()
    test_knowledge_config_default_path()
    test_knowledge_config_override_path()
    test_knowledge_config_safety_check_default()
    test_knowledge_config_safety_check_override()
    test_knowledge_config_safety_check_rejects_outside()
    print("\nAll isolation tests passed!")