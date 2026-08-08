"""
Progress Store
--------------
Lightweight in-memory store that tracks per-job ingestion progress.

Each job entry looks like:

    {
        "status":     "pending" | "processing" | "done" | "failed",
        "percent":    0-100,
        "sheet":      "Cash X Sale",   # currently active sheet name
        "rows_done":  4500,
        "total_rows": 10000,
        "message":    "Processing…",   # human-readable status line
        "result":     { ... } | None,  # final API payload on success
        "error":      "..." | None,    # error detail on failure
    }

This is intentionally a plain dict — no Redis, no DB.  For a single-process
uvicorn deployment this is safe.  If you ever run multiple workers, replace
this module with a Redis-backed equivalent.
"""

from __future__ import annotations

import threading
from typing import TypedDict

_lock  = threading.Lock()
_store: dict[str, dict] = {}


class JobProgress(TypedDict, total=False):
    status:     str
    percent:    int
    sheet:      str
    rows_done:  int
    total_rows: int
    message:    str
    result:     dict | None
    error:      str | None


def create_job(job_id: str) -> None:
    """Initialise a new job entry with 'pending' status."""
    with _lock:
        _store[job_id] = JobProgress(
            status="pending",
            percent=0,
            sheet="",
            rows_done=0,
            total_rows=0,
            message="Waiting to start…",
            result=None,
            error=None,
        )


def update_job(job_id: str, **kwargs) -> None:
    """Merge *kwargs* into the job entry.  No-op if job_id is unknown."""
    with _lock:
        if job_id in _store:
            _store[job_id].update(kwargs)


def get_job(job_id: str) -> dict | None:
    """Return a snapshot of the job entry, or None if not found."""
    with _lock:
        entry = _store.get(job_id)
        return dict(entry) if entry else None


def delete_job(job_id: str) -> None:
    """Remove a job entry (call after the SSE client disconnects)."""
    with _lock:
        _store.pop(job_id, None)
