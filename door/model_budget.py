"""Persisted daily inference admission, charged before a model request."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


class ModelLimitError(RuntimeError):
    pass


def admit(data_dir: str | Path, channel: str = "bot") -> None:
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    cap = max(0, int(os.getenv("MODEL_DAILY_CAP", "300")))
    with sqlite3.connect(path / "model_usage.sqlite", timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS model_usage(day TEXT NOT NULL,channel TEXT NOT NULL,calls INTEGER NOT NULL,PRIMARY KEY(day,channel))")
        db.execute("BEGIN IMMEDIATE")
        used = db.execute("SELECT COALESCE(SUM(calls),0) FROM model_usage WHERE day=?", (day,)).fetchone()[0]
        if used >= cap:
            raise ModelLimitError("The daily model call cap has been reached.")
        db.execute("INSERT INTO model_usage(day,channel,calls) VALUES(?,?,1) ON CONFLICT(day,channel) DO UPDATE SET calls=calls+1", (day, channel))
