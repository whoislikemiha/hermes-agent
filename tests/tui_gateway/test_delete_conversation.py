"""``_delete_conversation``: the one server-side delete every entry point calls.

The server owns live sessions, so it — not each client — stops and closes them before the rows go;
another process's open conversation is refused, never deleted out from under it.
"""

from __future__ import annotations

import os
import threading
import types

import pytest

from hermes_cli import active_sessions
from hermes_state import SessionDB
from hermes_state_errors import SessionOpenElsewhereError
from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    return tmp_path


@pytest.fixture
def db(home):
    store = SessionDB(db_path=home / "state.db")
    yield store
    store.close()


@pytest.fixture
def broadcasts(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "_broadcast_global_event", lambda event, payload=None: sent.append((event, payload)))
    return sent


def _conversation(db, sid="conv"):
    db.create_session(session_id=sid, source="desktop")
    db.append_message(sid, "user", "hello")


def _live(session_key, **extra):
    record = {
        "agent": None,
        "session_key": session_key,
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "running": False,
        "slash_worker": None,
    }
    record.update(extra)
    return record


def _register(sid, record):
    server._sessions[sid] = record
    return record


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for sid in [s for s in server._sessions if s.startswith("rt-")]:
        server._sessions.pop(sid, None)


def test_deletes_rows_closes_the_live_session_and_tells_clients(db, home, broadcasts):
    _conversation(db)
    committed = []
    agent = types.SimpleNamespace(session_id="conv", commit_memory_session=committed.append)
    _register("rt-1", _live("conv", agent=agent))
    record_turn_start(home, "conv", "unfinished prompt")

    assert server._delete_conversation(db, "conv", home=home) == ["conv"]

    assert "rt-1" not in server._sessions
    assert db.get_session("conv") is None
    assert committed == []  # a deleted conversation is not handed to the memory provider
    assert read_turn_marker(home, "conv") is None
    assert ("session.deleted", {"stored_session_ids": ["conv"], "runtime_session_ids": ["rt-1"]}) in broadcasts


def test_a_failed_agent_build_does_not_block_the_delete(db, home, broadcasts):
    """The reported bug: a conversation opened before sign-in kept its build error, and the stop step of
    the old client-driven delete returned that error instead of deleting."""
    _conversation(db)
    ready = threading.Event()
    ready.set()
    _register("rt-1", _live("conv", agent_ready=ready, agent_error="No Anthropic credentials found."))

    assert server._delete_conversation(db, "conv", home=home) == ["conv"]
    assert db.get_session("conv") is None
    assert "rt-1" not in server._sessions


def test_finds_a_live_session_whose_key_compression_rotated(db, home, broadcasts):
    db.create_session(session_id="root", source="desktop")
    db.end_session("root", "compression")
    db.create_session(session_id="tip", source="desktop", parent_session_id="root")
    _register("rt-1", _live("root", agent=types.SimpleNamespace(session_id="tip")))

    assert set(server._delete_conversation(db, "tip", home=home)) == {"root", "tip"}
    assert "rt-1" not in server._sessions


def test_leaves_another_profiles_live_session_alone(db, home, tmp_path_factory, broadcasts):
    _conversation(db)
    other_profile = tmp_path_factory.mktemp("other-profile")
    _register("rt-other", _live("conv", profile_home=str(other_profile)))

    server._delete_conversation(db, "conv", home=home)

    assert "rt-other" in server._sessions


def test_refuses_a_conversation_another_process_has_open(db, home, broadcasts):
    _conversation(db)
    owner = os.getppid()
    entry = active_sessions._lease_entry(lease_id="other-lease", session_id="conv", surface="tui")
    entry.update(pid=owner, process_start_time=active_sessions._process_start_time(owner))
    state_path, _lock = active_sessions._lease_paths(registry_home=home)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    active_sessions._write_entries(state_path, [entry])

    with pytest.raises(SessionOpenElsewhereError, match="open in another Hermes window"):
        server._delete_conversation(db, "conv", home=home)

    assert db.get_session("conv") is not None
    assert broadcasts == []


def test_unknown_id_is_a_no_op(db, home, broadcasts):
    assert server._delete_conversation(db, "missing", home=home) == []
    assert broadcasts == []


def test_bulk_tears_down_each_and_reports_skips(db, home, broadcasts):
    _conversation(db, "a")
    _conversation(db, "b")
    _register("rt-a", _live("a"))
    owner = os.getppid()
    entry = active_sessions._lease_entry(lease_id="other-lease", session_id="b", surface="tui")
    entry.update(pid=owner, process_start_time=active_sessions._process_start_time(owner))
    state_path, _lock = active_sessions._lease_paths(registry_home=home)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    active_sessions._write_entries(state_path, [entry])

    deleted, skipped = server._delete_conversations(db, ["a", "b", "missing"], home=home)

    assert (deleted, skipped) == (["a"], ["b"])
    assert "rt-a" not in server._sessions
    assert db.get_session("a") is None and db.get_session("b") is not None
    assert broadcasts == [("session.deleted", {"stored_session_ids": ["a"], "runtime_session_ids": ["rt-a"]})]
