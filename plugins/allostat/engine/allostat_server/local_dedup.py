"""Bounded local storage for the hosted engine's in-memory dedup cache.

Only session IDs, pillar names, body hashes and timestamps are stored. A
transaction serializes competing hooks; no account database is involved.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEDUP_WINDOW_SECONDS
from .runtime_context import local_dedup_state


@contextmanager
def transaction(*, now: datetime | None = None, window_seconds: int = DEDUP_WINDOW_SECONDS):
    configured = os.environ.get("ALLOSTAT_LOCAL_STATE_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".allostat" / "local-runtime"
    if not root.is_absolute():
        raise ValueError("ALLOSTAT_LOCAL_STATE_DIR must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "dedup.sqlite3", timeout=5)
    token = None
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS decisions (session TEXT, pillar TEXT, fingerprint TEXT, fired_at TEXT NOT NULL, PRIMARY KEY (session, pillar, fingerprint))")
        conn.execute("BEGIN IMMEDIATE")
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=window_seconds)
        state = {}
        for session, pillar, fingerprint, fired_at in conn.execute("SELECT session, pillar, fingerprint, fired_at FROM decisions"):
            stamp = datetime.fromisoformat(fired_at)
            if stamp > cutoff:
                state[(session, pillar, fingerprint)] = stamp
        token = local_dedup_state.set(state)
        yield
        conn.execute("DELETE FROM decisions")
        conn.executemany("INSERT INTO decisions VALUES (?, ?, ?, ?)", [(*key, stamp.isoformat()) for key, stamp in state.items()])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if token is not None:
            local_dedup_state.reset(token)
        conn.close()
