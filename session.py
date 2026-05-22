"""
wisp-agent session persistence.
Saves/restores conversation history to ~/.wisp/sessions/
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_SESSIONS_DIR = Path.home() / ".wisp" / "sessions"


def _ensure() -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def new_id() -> str:
    """Generate a new session ID based on current timestamp."""
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def save(session_id: str, messages: list, name: str = "") -> None:
    """Save (or update) a session to disk."""
    _ensure()
    path = _SESSIONS_DIR / f"{session_id}.json"
    now = datetime.now().isoformat()
    created = now
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            created = existing.get("created", now)
            if not name:
                name = existing.get("name", "")
        except Exception:
            pass
    data = {
        "id":            session_id,
        "name":          name,
        "created":       created,
        "updated":       now,
        "message_count": len(messages),
        "messages":      messages,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load(session_id_or_name: str) -> dict | None:
    """Load a session by ID or display name."""
    _ensure()
    # Direct ID match
    path = _SESSIONS_DIR / f"{session_id_or_name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    # Name match (scan all)
    for f in _SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("name") == session_id_or_name:
                return data
        except Exception:
            continue
    return None


def list_all() -> list[dict]:
    """Return summary of all sessions, newest first."""
    _ensure()
    result = []
    for f in sorted(_SESSIONS_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            result.append({
                "id":            data["id"],
                "name":          data.get("name", ""),
                "created":       data.get("created", ""),
                "updated":       data.get("updated", ""),
                "message_count": data.get("message_count", 0),
            })
        except Exception:
            continue
    return result


def count() -> int:
    """Return number of saved sessions."""
    _ensure()
    return len(list(_SESSIONS_DIR.glob("*.json")))
