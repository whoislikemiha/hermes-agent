from __future__ import annotations

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from tools.environments.local import _sanitize_subprocess_env


def test_real_gateway_session_provisions_exact_session_subprocess_capability(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_name="Builds",
        chat_type="thread",
        user_id="user-1",
        user_name="Builder",
        thread_id="thread-1",
        scope_id="guild-1",
        parent_chat_id="channel-1",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_id="20260719_120000_deadbeef",
        session_key="agent:main:discord:thread:thread-1",
    )

    tokens = runner._set_session_env(context)
    try:
        child_env = _sanitize_subprocess_env({"PATH": "/usr/bin"})
    finally:
        runner._clear_session_env(tokens)

    token_file = child_env["HERMES_EXTERNAL_EVENTS_TOKEN_FILE"]
    assert token_file.startswith(str(tmp_path / "external_events" / "capabilities"))
    assert "channel-1" not in token_file
    assert "thread-1" not in token_file


def test_unbound_and_non_gateway_bindings_receive_no_capability(monkeypatch):
    monkeypatch.setenv(
        "HERMES_EXTERNAL_EVENTS_TOKEN_FILE", "/foreign/session/capability"
    )

    tokens = set_session_vars(platform="api_server", session_id="api-session")
    try:
        child_env = _sanitize_subprocess_env(
            {"HERMES_EXTERNAL_EVENTS_TOKEN_FILE": "/foreign/session/capability"}
        )
    finally:
        clear_session_vars(tokens)

    assert child_env.get("HERMES_EXTERNAL_EVENTS_TOKEN_FILE", "") == ""


def test_capability_issuance_failure_does_not_crash_hot_turn(monkeypatch):
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="dm",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_id="session-1",
        session_key="agent:main:discord:dm:channel-1",
    )
    monkeypatch.setattr(
        "external_events.issue_session_capability",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    tokens = runner._set_session_env(context)
    try:
        child_env = _sanitize_subprocess_env({})
    finally:
        runner._clear_session_env(tokens)

    assert child_env["HERMES_EXTERNAL_EVENTS_TOKEN_FILE"] == ""
