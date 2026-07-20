from __future__ import annotations

import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gateway.external_events import (
    MAX_PAYLOAD_BYTES,
    ExternalEventError,
    issue_session_capability,
    publish_event,
    validate_session_capability,
)


def _capability(home: Path) -> Path:
    return issue_session_capability(
        session_id="20260719_120000_deadbeef",
        session_key="agent:main:telegram:dm:chat-1",
        profile_home=home,
    )


def test_capability_is_private_profile_local_and_reused(tmp_path):
    token_file = _capability(tmp_path)
    original = token_file.read_bytes()

    reused = issue_session_capability(
        session_id="20260719_120000_deadbeef",
        session_key="agent:main:telegram:dm:chat-1",
        profile_home=tmp_path,
    )

    assert reused == token_file
    assert reused.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    binding = validate_session_capability(str(token_file), tmp_path)
    assert binding.profile_home == str(tmp_path.resolve())
    assert binding.session_id == "20260719_120000_deadbeef"
    assert binding.session_key == "agent:main:telegram:dm:chat-1"


def test_capability_binding_change_gets_distinct_path_and_profile_is_exact(tmp_path):
    token_file = _capability(tmp_path / "profile-a")

    rebound = issue_session_capability(
        session_id="20260719_120000_deadbeef",
        session_key="agent:main:telegram:dm:other",
        profile_home=tmp_path / "profile-a",
    )
    assert rebound != token_file
    with pytest.raises(ExternalEventError, match="active profile"):
        validate_session_capability(str(token_file), tmp_path / "profile-b")


def test_concurrent_publishers_create_unique_pending_ledger_rows(tmp_path):
    token_file = _capability(tmp_path)

    def publish(index: int) -> str:
        return publish_event(
            {"index": index}, token_file=str(token_file), profile_home=tmp_path
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        event_ids = list(pool.map(publish, range(80)))

    assert len(set(event_ids)) == 80
    with sqlite3.connect(tmp_path / "state.db") as conn:
        rows = conn.execute(
            "SELECT completion_type, delivery_state, event_json FROM async_delegations"
        ).fetchall()
    assert len(rows) == 80
    assert {row[0] for row in rows} == {"external_event"}
    assert {row[1] for row in rows} == {"pending"}
    events = [json.loads(row[2]) for row in rows]
    assert {item["payload"]["index"] for item in events} == set(range(80))
    assert {item["parent_session_id"] for item in events} == {
        "20260719_120000_deadbeef"
    }


def test_payload_limit_constant_is_64_kib():
    assert MAX_PAYLOAD_BYTES == 64 * 1024


def test_publish_accepts_payload_at_serialized_size_limit(tmp_path):
    token_file = _capability(tmp_path)
    payload = {"data": "x" * (MAX_PAYLOAD_BYTES - len('{"data":""}'))}

    event_id = publish_event(
        payload, token_file=str(token_file), profile_home=tmp_path
    )

    with sqlite3.connect(tmp_path / "state.db") as conn:
        stored = conn.execute(
            "SELECT event_json FROM async_delegations WHERE delegation_id=?",
            (event_id,),
        ).fetchone()
    assert json.loads(stored[0])["payload"] == payload


def test_publish_rejects_payload_over_serialized_size_limit(tmp_path):
    token_file = _capability(tmp_path)
    payload = {"data": "x" * (MAX_PAYLOAD_BYTES - len('{"data":""}') + 1)}

    with pytest.raises(ExternalEventError, match="64 KiB"):
        publish_event(payload, token_file=str(token_file), profile_home=tmp_path)

    assert not (tmp_path / "state.db").exists()


def test_publish_isolated_to_capability_profile(tmp_path):
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    token_a = _capability(profile_a)
    token_b = _capability(profile_b)

    publish_event({"profile": "a"}, token_file=str(token_a), profile_home=profile_a)
    publish_event({"profile": "b"}, token_file=str(token_b), profile_home=profile_b)

    for home, expected in ((profile_a, "a"), (profile_b, "b")):
        with sqlite3.connect(home / "state.db") as conn:
            payload = conn.execute(
                "SELECT event_json FROM async_delegations"
            ).fetchone()[0]
        assert json.loads(payload)["payload"] == {"profile": expected}


def test_capability_cleanup_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.external_events._MAX_CAPABILITIES", 3)

    for index in range(5):
        issue_session_capability(
            session_id=f"session-{index}",
            session_key=f"agent:main:telegram:dm:{index}",
            profile_home=tmp_path,
        )

    capabilities = list((tmp_path / "external_events" / "capabilities").glob("*.json"))
    assert len(capabilities) == 3


def test_reissued_capability_refreshes_recency_before_pruning(monkeypatch, tmp_path):
    monkeypatch.setattr("gateway.external_events._MAX_CAPABILITIES", 3)
    paths = []
    for index in range(3):
        paths.append(
            issue_session_capability(
                session_id=f"session-{index}",
                session_key=f"agent:main:telegram:dm:{index}",
                profile_home=tmp_path,
            )
        )
    for index, path in enumerate(paths):
        os.utime(path, (100 + index, 100 + index))

    refreshed = issue_session_capability(
        session_id="session-0",
        session_key="agent:main:telegram:dm:0",
        profile_home=tmp_path,
    )
    issue_session_capability(
        session_id="session-3",
        session_key="agent:main:telegram:dm:3",
        profile_home=tmp_path,
    )

    assert refreshed.exists()
    assert not paths[1].exists()
    if os.name != "nt":
        assert stat.S_IMODE(refreshed.stat().st_mode) == 0o600
