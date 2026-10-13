from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Job, Plan
from .security import Cipher


class Store:
    def __init__(self, path: Path, cipher: Cipher) -> None:
        self.path, self.cipher = path, cipher
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, payload BLOB NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
                """
            )
            # Plans written by v0.1.0 before the DPAPI compatibility fix stored
            # an integer instead of the protected snapshot. They cannot be
            # recovered and are discarded before an operator can run them.
            db.execute("DELETE FROM plans WHERE typeof(payload) != 'blob'")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def save_plan(self, plan: Plan) -> None:
        payload = self.cipher.protect(json.dumps(plan.to_dict()).encode())
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO plans VALUES (?, ?, ?)", (plan.id, sqlite3.Binary(payload), self._now()))

    def get_plan(self, plan_id: str) -> Plan:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not row:
            raise KeyError(f"Plano {plan_id} não encontrado")
        return Plan.from_dict(json.loads(self.cipher.unprotect(row[0])))

    def save_job(self, job: Job) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)", (job.id, job.plan_id, json.dumps(job.to_dict()), self._now()))

    def get_job(self, job_id: str) -> Job:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"Job {job_id} não encontrado")
        return Job.from_dict(json.loads(row[0]))

    def event(self, job_id: str, level: str, message: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO events(job_id, at, level, message) VALUES (?, ?, ?, ?)", (job_id, self._now(), level, message))

    def events_since(self, job_id: str, event_id: int = 0) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id, at, level, message FROM events WHERE job_id = ? AND id > ? ORDER BY id", (job_id, event_id)).fetchall()
        return [dict(id=r[0], at=r[1], level=r[2], message=r[3]) for r in rows]
