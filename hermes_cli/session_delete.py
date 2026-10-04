"""Delete stored conversations, refusing ones another Hermes process has open.

The process-agnostic half of a delete. Every deleter (the Desktop/dashboard backend, the TUI gateway,
the ``hermes sessions`` CLI) shares two things: ``state.db`` and the active-session registry, which
records at most one live owner per conversation. Only the owning process can tear a live session down,
so the backend first tears down its own (``tui_gateway`` ``_delete_conversation``) and then calls in
here; the CLI owns nothing and calls in here directly. A conversation open in another process is
refused, never deleted out from under it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from hermes_state_errors import SessionOpenElsewhereError


def _open_elsewhere(home: Path) -> dict[str, dict[str, Any]]:
    """``session_id -> registry entry`` for sessions another live process holds in *home*'s registry.
    Fails closed: an unreadable registry cannot prove the conversation is free."""
    from hermes_cli.active_sessions import ActiveSessionRegistryError, active_session_registry_snapshot

    try:
        # An unreadable registry, or an owner whose liveness can't be determined, raises: refusing is the safe
        # side, a delete can't be undone.
        entries = active_session_registry_snapshot(home)
    except ActiveSessionRegistryError as exc:
        raise SessionOpenElsewhereError(
            f"Hermes could not check whether another window has this chat open: {exc}") from exc
    return {str(e.get("session_id") or ""): e for e in entries if e.get("pid") != os.getpid()}


def _refusal(session_id: str, ids: Iterable[str], open_elsewhere: dict[str, dict[str, Any]]) -> str | None:
    from hermes_cli.active_sessions import session_already_owned_message

    for sid in ids:
        if (entry := open_elsewhere.get(sid)) is not None:
            return session_already_owned_message(session_id, entry)
    return None


def _clear_turn_markers(ids: Iterable[str], home: Path) -> None:
    """A crash-recovery marker would auto-continue a turn of a conversation that no longer exists."""
    from tui_gateway.turn_marker import clear_turn_marker

    for sid in ids:
        clear_turn_marker(home, sid)


def delete_stored_conversation(db, session_id: str, *, home: Path) -> list[str]:
    """Delete *session_id*'s whole conversation (every compression segment and delegate child), its
    transcript files and crash markers. Returns the removed ids, ``[]`` when the id is unknown.

    Raises :class:`SessionOpenElsewhereError` when another process has the conversation open, and
    :class:`SessionActiveWriteGuardError` (its base) when a turn or compression is still writing it.
    The caller must already have torn down any live session it owns itself."""
    home = Path(home)
    ids = db.get_session_delete_targets(session_id)
    if not ids:
        return []
    if (refusal := _refusal(session_id, ids, _open_elsewhere(home))) is not None:
        raise SessionOpenElsewhereError(refusal)
    if not db.delete_session(session_id, sessions_dir=home / "sessions", exclude_active_write_guards=True):
        return []
    _clear_turn_markers(ids, home)
    return ids


def delete_stored_conversations(db, session_ids: Iterable[str], *, home: Path) -> tuple[list[str], list[str]]:
    """Bulk form of :func:`delete_stored_conversation`, rows removed in one transaction. Returns
    ``(deleted, skipped)`` as selected ids: a conversation open in another process or still being written
    is skipped rather than failing the whole selection."""
    home = Path(home)
    targets = {sid: db.get_session_delete_targets(sid) for sid in dict.fromkeys(session_ids) if sid}
    targets = {sid: ids for sid, ids in targets.items() if ids}
    open_elsewhere = _open_elsewhere(home) if targets else {}
    skipped = [sid for sid, ids in targets.items() if _refusal(sid, ids, open_elsewhere) is not None]
    remaining = [sid for sid in targets if sid not in skipped]
    guarded: list[str] = []
    if remaining:
        db.delete_sessions(remaining, sessions_dir=home / "sessions", exclude_active_write_guards=True,
                           skipped_ids=guarded)
    deleted = [sid for sid in remaining if sid not in guarded]
    _clear_turn_markers((i for sid in deleted for i in targets[sid]), home)
    return deleted, skipped + guarded
