from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from gateway.external_events import TOKEN_ENV_VAR, issue_session_capability
from hermes_cli.subcommands.events import publish_from_stdin


class _Stdin:
    def __init__(self, value: bytes):
        self.buffer = io.BytesIO(value)


def _bind(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    token_file = issue_session_capability(
        session_id="session-1",
        session_key="agent:main:discord:dm:chat-1",
        profile_home=tmp_path,
    )
    monkeypatch.setenv(TOKEN_ENV_VAR, str(token_file))
    return token_file


def test_publish_reads_only_json_from_stdin(monkeypatch, tmp_path, capsys):
    _bind(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", _Stdin(b'{"kind":"build.complete","ok":true}'))

    publish_from_stdin()

    output = capsys.readouterr()
    assert output.out == "Event queued.\n"
    assert output.err == ""
    with sqlite3.connect(tmp_path / "state.db") as conn:
        row = conn.execute(
            "SELECT completion_type, delivery_state, event_json FROM async_delegations"
        ).fetchone()
    assert row[:2] == ("external_event", "pending")
    envelope = json.loads(row[2])
    assert envelope["payload"] == {"kind": "build.complete", "ok": True}


@pytest.mark.parametrize("value", [b"[]", b"null", b"{} trailing", b"\xff"])
def test_publish_rejects_invalid_input(monkeypatch, tmp_path, value, capsys):
    _bind(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", _Stdin(value))

    with pytest.raises(SystemExit) as raised:
        publish_from_stdin()

    assert raised.value.code == 2
    assert "token" not in capsys.readouterr().err.lower()


def test_publish_rejects_missing_capability_without_route_details(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr("sys.stdin", _Stdin(b'{"ok":true}'))

    with pytest.raises(SystemExit) as raised:
        publish_from_stdin()

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Error: unable to publish the event.\n"


def test_publish_rejects_input_over_64_kib(monkeypatch, tmp_path):
    _bind(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", _Stdin(b"{" + b" " * (64 * 1024)))

    with pytest.raises(SystemExit) as raised:
        publish_from_stdin()

    assert raised.value.code == 2
