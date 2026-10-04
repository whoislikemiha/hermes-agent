"""``SessionDB.turn_counts``: the size the desktop shows for a conversation.

``message_count`` counts every stored row, so one prompt that runs eight tools
already reads as ~18 "messages". A turn is a prompt the user typed, counted
the way a display read paints it.
"""
import json

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _tool_turn(db, sid, prompt, calls=2):
    """One user prompt answered through ``calls`` tool round-trips."""
    db.append_message(session_id=sid, role="user", content=prompt)
    for i in range(calls):
        call_id = f"{prompt}-{i}"
        db.append_message(session_id=sid, role="assistant", content="",
                          tool_calls=[{"id": call_id, "type": "function",
                                       "function": {"name": "terminal", "arguments": json.dumps({"i": i})}}])
        db.append_message(session_id=sid, role="tool", content="ok", tool_call_id=call_id, tool_name="terminal")
    db.append_message(session_id=sid, role="assistant", content=f"answer to {prompt}")


def test_counts_prompts_not_tool_rows(db):
    db.create_session(session_id="s", source="desktop")
    _tool_turn(db, "s", "first", calls=3)
    _tool_turn(db, "s", "second", calls=2)

    assert db.get_session("s")["message_count"] == 14
    assert db.turn_counts(["s"]) == {"s": 2}


def test_backend_notices_and_steers_are_not_turns(db):
    db.create_session(session_id="s", source="desktop")
    _tool_turn(db, "s", "real prompt")
    for kind in ("process_complete", "async_delegation_complete", "model_switch", "auto_continue", "steer"):
        db.append_message(session_id="s", role="user", content=f"[{kind}]", display_kind=kind)

    assert db.turn_counts(["s"]) == {"s": 1}


def test_rewound_prompt_is_not_counted(db):
    db.create_session(session_id="s", source="desktop")
    _tool_turn(db, "s", "kept")
    _tool_turn(db, "s", "undone")
    undone = next(m for m in db.get_messages("s") if m["content"] == "undone")
    db.rewind_to_message("s", undone["id"])

    assert db.turn_counts(["s"]) == {"s": 1}


def test_compaction_carry_counts_once(db):
    db.create_session(session_id="s", source="desktop")
    old = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
           {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]
    db.append_messages_batch("s", old)
    db.archive_and_compact("s", [{"role": "assistant", "content": "summary"}, *old[2:]], tail_count=2)
    db.append_message(session_id="s", role="user", content="q3")

    assert db.turn_counts(["s"]) == {"s": 3}


def test_archived_and_live_copy_of_one_prompt_count_once(db):
    """An older compaction generation left the archived original AND a live copy visible; the
    store folds both into one ``display_order`` group (#122167), and the display read paints one."""
    db.create_session(session_id="s", source="desktop")
    _tool_turn(db, "s", "carried prompt", calls=1)
    db.append_message(session_id="s", role="user", content="later prompt")
    original = next(m for m in db.get_messages("s") if m["content"] == "carried prompt")
    with db._lock:
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(messages)")
                if r[1] not in ("id", "active", "compacted", "display_order")]
        db._conn.execute(  # the compactor's pure-SQL clone: every column but id, fresh display_order
            f"INSERT INTO messages ({', '.join(cols)}) SELECT {', '.join(cols)} FROM messages WHERE id = ?",
            (original["id"],))
        db._conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (original["id"],))
        db._conn.commit()

    visible = db._conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT display_order) FROM messages WHERE session_id = 's'"
        " AND content = 'carried prompt' AND (active = 1 OR compacted = 1)").fetchone()
    assert tuple(visible) == (2, 1), "precondition: two visible rows, one display group"
    painted = [m["content"] for m in db.get_messages("s", include_compacted=True) if m["role"] == "user"]
    assert painted.count("carried prompt") == 1
    assert db.turn_counts(["s"]) == {"s": 2}


def test_legacy_rows_without_display_order_count_individually(db):
    db.create_session(session_id="s", source="desktop")
    for prompt in ("one", "two", "three"):
        _tool_turn(db, "s", prompt, calls=1)
    with db._lock:
        db._conn.execute("UPDATE messages SET display_order = NULL WHERE session_id = 's'")
        db._conn.commit()

    assert db.turn_counts(["s"]) == {"s": 3}


def test_one_query_per_page_not_per_session(db, monkeypatch):
    """The sidebar lists up to 500 rows per slice: a per-session COUNT would be N+1."""
    for i in range(30):
        db.create_session(session_id=f"s{i}", source="desktop")
        db.append_message(session_id=f"s{i}", role="user", content=f"hi {i}")
    calls = []
    real = db._read_all
    monkeypatch.setattr(db, "_read_all", lambda sql, params=(): (calls.append(sql), real(sql, params))[1])

    counts = db.turn_counts([f"s{i}" for i in range(30)])

    assert set(counts.values()) == {1} and len(counts) == 30
    assert len(calls) == 1


def test_unknown_and_empty_ids(db):
    db.create_session(session_id="empty", source="desktop")

    assert db.turn_counts(["empty", "missing", "", "empty"]) == {"empty": 0, "missing": 0}
    assert db.turn_counts([]) == {}


def test_list_sessions_rich_turn_counts_are_opt_in(db):
    db.create_session(session_id="s", source="desktop")
    _tool_turn(db, "s", "hello", calls=4)

    [plain] = db.list_sessions_rich(limit=10, compact_rows=True)
    assert "turn_count" not in plain
    [row] = db.list_sessions_rich(limit=10, compact_rows=True, include_turn_counts=True)
    assert (row["message_count"], row["turn_count"]) == (10, 1)


def test_list_turn_count_follows_the_compression_tip(db):
    """The listed row surfaces the tip's id and counts, so ``turn_count`` must too."""
    db.create_session(session_id="root", source="desktop")
    _tool_turn(db, "root", "before compression")
    db.end_session("root", "compression")
    db.create_session(session_id="tip", source="desktop", parent_session_id="root")
    _tool_turn(db, "tip", "after one")
    _tool_turn(db, "tip", "after two")

    [row] = db.list_sessions_rich(limit=10, compact_rows=True, order_by_last_active=True,
                                  include_turn_counts=True)
    assert (row["id"], row["turn_count"]) == ("tip", 2)
