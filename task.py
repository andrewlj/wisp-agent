"""
wisp-agent task checkpoint system.
Tracks multi-step task progress to ~/.wisp/tasks/
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_TASKS_DIR = Path.home() / "wisp" / "tasks"

_current_task_id:     str | None = None
_current_session_id:  str | None = None

_ICONS = {
    "done":        "✓",
    "failed":      "✗",
    "in_progress": "►",
    "pending":     "○",
    "skipped":     "—",
}


def _ensure() -> None:
    _TASKS_DIR.mkdir(parents=True, exist_ok=True)


def set_session_id(sid: str) -> None:
    """Called by agent.py when a session starts, so new tasks are linked."""
    global _current_session_id
    _current_session_id = sid


def get_current_id() -> str | None:
    return _current_task_id


def set_current_id(task_id: str | None) -> None:
    global _current_task_id
    _current_task_id = task_id


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create(title: str, steps: list[str]) -> dict:
    """Create a new task with the given steps. Returns the full task dict."""
    global _current_task_id
    _ensure()
    task_id = f"task-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    now = datetime.now().isoformat()
    task = {
        "id":         task_id,
        "title":      title,
        "status":     "in_progress",
        "created":    now,
        "updated":    now,
        "session_id": _current_session_id or "",
        "steps": [
            {
                "id":           i + 1,
                "title":        s,
                "status":       "pending",
                "note":         "",
                "completed_at": None,
            }
            for i, s in enumerate(steps)
        ],
    }
    (_TASKS_DIR / f"{task_id}.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2)
    )
    _current_task_id = task_id
    return task


def _load_file(task_id: str) -> tuple[dict | None, Path]:
    path = _TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        return None, path
    try:
        return json.loads(path.read_text()), path
    except Exception:
        return None, path


def step_done(task_id: str, step_id: int, note: str = "") -> str:
    """Mark a step as completed."""
    task, path = _load_file(task_id)
    if task is None:
        return f"error: task '{task_id}' not found"
    for step in task["steps"]:
        if step["id"] == step_id:
            step["status"] = "done"
            step["note"] = note
            step["completed_at"] = datetime.now().isoformat()
            break
    else:
        return f"error: step {step_id} not found in task '{task_id}'"
    if all(s["status"] in ("done", "skipped") for s in task["steps"]):
        task["status"] = "done"
    task["updated"] = datetime.now().isoformat()
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2))
    done  = sum(1 for s in task["steps"] if s["status"] == "done")
    total = len(task["steps"])
    return f"ok: step {step_id} done ({done}/{total} completed)"


def step_fail(task_id: str, step_id: int, reason: str = "") -> str:
    """Mark a step as failed and set the task status to interrupted."""
    task, path = _load_file(task_id)
    if task is None:
        return f"error: task '{task_id}' not found"
    for step in task["steps"]:
        if step["id"] == step_id:
            step["status"] = "failed"
            step["note"] = reason
            break
    else:
        return f"error: step {step_id} not found"
    task["status"] = "interrupted"
    task["updated"] = datetime.now().isoformat()
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2))
    return f"ok: step {step_id} marked as failed"


def load(task_id: str) -> dict | None:
    task, _ = _load_file(task_id)
    return task


def list_all() -> list[dict]:
    """Return all tasks, newest first."""
    _ensure()
    result = []
    for f in sorted(_TASKS_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            result.append(json.loads(f.read_text()))
        except Exception:
            continue
    return result


# ── Formatting ────────────────────────────────────────────────────────────────

def format_resume_context(task: dict) -> str:
    """Build the context string injected when resuming a task."""
    done  = sum(1 for s in task["steps"] if s["status"] == "done")
    total = len(task["steps"])
    lines = [
        "[Task Resume]",
        f"Task: {task['title']}",
        f"Progress: {done}/{total} steps done",
        "",
    ]
    for s in task["steps"]:
        icon = _ICONS.get(s["status"], "?")
        line = f"{icon} Step {s['id']}: {s['title']}"
        if s.get("note"):
            line += f"  — {s['note']}"
        lines.append(line)
    next_step = next(
        (s for s in task["steps"] if s["status"] in ("pending", "failed")), None
    )
    if next_step:
        lines.append(
            f"\nPlease continue from Step {next_step['id']}: {next_step['title']}"
        )
    return "\n".join(lines)


def format_list() -> str:
    """Return a formatted table of all tasks for REPL display."""
    tasks = list_all()
    if not tasks:
        return "no saved tasks"
    status_icons = {"done": "✓", "in_progress": "►", "interrupted": "✗", "pending": "○"}
    lines = []
    for t in tasks:
        done  = sum(1 for s in t["steps"] if s["status"] == "done")
        total = len(t["steps"])
        icon  = status_icons.get(t["status"], "?")
        ts    = t["updated"][:16].replace("T", " ")
        lines.append(f"  {icon}  {t['id']}  [{done}/{total}]  {t['title']}  ({ts})")
    return "\n".join(lines)
