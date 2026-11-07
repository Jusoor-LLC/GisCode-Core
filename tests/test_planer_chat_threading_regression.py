"""Regression coverage for a project ChatStore shared with Planner workers."""
from __future__ import annotations

import json
import threading

from giscode.inference.runtime import ChatResponse
from giscode.planer import PlanerAI
from giscode.projects import Project, ProjectStore
from giscode.projects.workspace import ProjectWorkspaceSession
from giscode.storage import ChatStore


class _StubInference:
    def chat_generate(self, request):
        return ChatResponse(text=json.dumps({"assistant_message": "worker reply"}))


def _run_worker(function):
    values = []
    errors = []

    def run():
        try:
            values.append(function())
        except Exception as exc:  # surfaced on the test thread
            errors.append(exc)

    worker = threading.Thread(target=run, name="planer-chat-regression-worker")
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "Planner worker did not finish"
    assert not errors, f"Planner worker raised: {errors!r}"
    return values[0] if values else None


def _session(tmp_path):
    project_store = ProjectStore(db_path=str(tmp_path / "projects.db"))
    chat_store = ChatStore(db_path=str(tmp_path / "chats.db"))
    project = Project.create("Worker Chat", str(tmp_path / "project"))
    project_store.create_project(project)
    session = ProjectWorkspaceSession.open(project_store, chat_store, project.id)
    session._ai = PlanerAI(engine=session.engine, inference=_StubInference())
    return project_store, chat_store, session


def test_project_planner_turn_persists_from_worker_thread(tmp_path):
    """Session/store created on the test thread can persist a Planner turn
    from the GUI-style worker thread using the same ChatStore instance.
    """
    project_store, chat_store, session = _session(tmp_path)
    try:
        main_thread_id = threading.get_ident()

        def planner_turn_from_worker():
            store_connection_id = id(session.planer_chat.chat_store._conn)
            worker_thread_id = threading.get_ident()
            result = session.chat_with_planer("Inspect transferred projects")
            return store_connection_id, worker_thread_id, result

        connection_id, worker_thread_id, result = _run_worker(planner_turn_from_worker)
        assert result.success is True
        assert worker_thread_id != main_thread_id
        assert connection_id == id(chat_store._conn)

        history = session.load_chat_history()
        assert [(message.role, message.content) for message in history] == [
            ("user", "Inspect transferred projects"),
            ("assistant", "worker reply"),
        ]
    finally:
        session.close()
        chat_store.close()
        project_store.close()


def test_project_chat_listing_works_from_worker_thread(tmp_path):
    project_store, chat_store, session = _session(tmp_path)
    try:
        expected = session.planer_chat.get_or_create_chat()
        session.planer_chat.clear_cached_chat_id()

        listed = _run_worker(session.planer_chat.find_existing_chat)

        assert listed is not None
        assert listed.id == expected.id
    finally:
        session.close()
        chat_store.close()
        project_store.close()


def test_same_thread_planner_persistence_is_preserved(tmp_path):
    project_store, chat_store, session = _session(tmp_path)
    try:
        result = session.chat_with_planer("Same-thread request")

        assert result.success is True
        assert [message.content for message in session.load_chat_history()] == [
            "Same-thread request",
            "worker reply",
        ]
    finally:
        session.close()
        chat_store.close()
        project_store.close()


def test_concurrent_planner_writers_preserve_every_turn(tmp_path):
    project_store, chat_store, session = _session(tmp_path)
    try:
        # Materialize the chat before concurrency so each worker has its own
        # persistence adapter for the same existing project chat.
        session.planer_chat.get_or_create_chat()
        project = session.project
        worker_errors = []
        worker_lock = threading.Lock()

        def worker(worker_id: int):
            from giscode.planer.chat import PlanerChatPersistence

            persistence = PlanerChatPersistence(project, chat_store)
            try:
                for turn in range(5):
                    persistence.record_user_turn(
                        f"worker-{worker_id}-user-{turn}",
                        turn_id=f"worker-{worker_id}-user-{turn}",
                    )
                    persistence.record_assistant_turn(
                        f"worker-{worker_id}-assistant-{turn}",
                        turn_id=f"worker-{worker_id}-assistant-{turn}",
                    )
            except Exception as exc:
                with worker_lock:
                    worker_errors.append(exc)

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(timeout=10)

        assert all(not worker_thread.is_alive() for worker_thread in workers)
        assert worker_errors == []
        history = session.load_chat_history()
        assert len(history) == 30
        turn_ids = [message.meta["turn_id"] for message in history]
        assert len(set(turn_ids)) == 30
    finally:
        session.close()
        chat_store.close()
        project_store.close()
