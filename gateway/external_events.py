"""Session-bound capabilities for durable external completion events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from tools.async_delegation import persist_durable_completion


TOKEN_ENV_VAR = "HERMES_EXTERNAL_EVENTS_TOKEN_FILE"
MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_CAPABILITIES = 256


class ExternalEventError(RuntimeError):
    """Raised when an external-event capability is invalid."""


@dataclass(frozen=True)
class EventCapability:
    profile_home: str
    session_id: str
    session_key: str


def _resolved(path: Path) -> str:
    return str(path.expanduser().resolve())


def _capability_dir(home: Path) -> Path:
    return home / "external_events" / "capabilities"


def _cleanup_capabilities(directory: Path) -> None:
    try:
        files = sorted(
            (path for path in directory.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[_MAX_CAPABILITIES:]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def issue_session_capability(
    *,
    session_id: str,
    session_key: str,
    profile_home: Path | None = None,
) -> Path:
    """Issue a private return capability bound to one durable session."""
    if not session_id or not session_key:
        raise ExternalEventError("A complete gateway session binding is required")

    home = Path(profile_home) if profile_home is not None else get_hermes_home()
    directory = _capability_dir(home)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{session_id}\0{session_key}".encode()).hexdigest()
    path = directory / f"{digest}.json"
    binding = {
        "profile_home": _resolved(home),
        "session_id": session_id,
        "session_key": session_key,
    }
    body = json.dumps(binding, ensure_ascii=False, separators=(",", ":")).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            existing_fd = os.open(path, flags)
            try:
                if not stat.S_ISREG(os.fstat(existing_fd).st_mode):
                    raise ExternalEventError("Event capability is not a regular file")
                if os.name != "nt":
                    os.fchmod(existing_fd, 0o600)
                os.utime(existing_fd, None)
            finally:
                os.close(existing_fd)
        except OSError as exc:
            raise ExternalEventError("Unable to refresh the event capability") from exc
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    _cleanup_capabilities(directory)
    return path


def validate_session_capability(
    token_file: str,
    profile_home: Path | None = None,
) -> EventCapability:
    """Validate possession of a profile-local capability file."""
    if not token_file:
        raise ExternalEventError(f"{TOKEN_ENV_VAR} is not set")
    home = Path(profile_home) if profile_home is not None else get_hermes_home()
    capability_dir = _capability_dir(home).resolve()
    try:
        path = Path(token_file).expanduser().resolve(strict=True)
        if path.parent != capability_dir:
            raise ExternalEventError("Event capability is outside the active profile")
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise ExternalEventError("Event capability is not a regular file")
        if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ExternalEventError("Event capability permissions are not private")
        data = json.loads(path.read_text(encoding="utf-8"))
    except ExternalEventError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalEventError("Event capability is invalid") from exc

    if (
        data.get("profile_home") != _resolved(home)
        or not isinstance(data.get("session_id"), str)
        or not data["session_id"]
        or not isinstance(data.get("session_key"), str)
        or not data["session_key"]
    ):
        raise ExternalEventError("Event capability binding is invalid")
    return EventCapability(
        profile_home=data["profile_home"],
        session_id=data["session_id"],
        session_key=data["session_key"],
    )


def publish_event(
    payload: dict[str, Any],
    *,
    token_file: str,
    profile_home: Path | None = None,
) -> str:
    if not isinstance(payload, dict):
        raise ExternalEventError("Event payload must be a JSON object")
    try:
        payload_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExternalEventError("Event payload must be JSON serializable") from exc
    if payload_size > MAX_PAYLOAD_BYTES:
        raise ExternalEventError("Event payload exceeds the 64 KiB limit")
    capability = validate_session_capability(token_file, profile_home)
    event_id = uuid.uuid4().hex
    event = {
        "type": "external_event",
        "event_id": event_id,
        "session_key": capability.session_key,
        "parent_session_id": capability.session_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    try:
        persist_durable_completion(
            event_id,
            event,
            completion_type="external_event",
            session_key=capability.session_key,
            parent_session_id=capability.session_id,
            profile_home=Path(capability.profile_home),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise ExternalEventError("Unable to persist the external event") from exc
    return event_id
