from __future__ import annotations

import os
from dataclasses import dataclass

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS autobuy_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    plan_label TEXT NOT NULL,
    interval_min INTEGER NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1,
    bought_count INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT,
    last_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class AutobuyJob:
    id: int
    plan_id: str
    service_name: str
    plan_label: str
    interval_min: int
    enabled: bool
    bought_count: int
    last_run_at: str | None
    last_status: str | None


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def add_job(self, plan_id: str, service_name: str, plan_label: str, interval_min: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO autobuy_jobs (plan_id, service_name, plan_label, interval_min) VALUES (?, ?, ?, ?)",
                (plan_id, service_name, plan_label, interval_min),
            )
            await db.commit()
            return cur.lastrowid

    async def list_jobs(self, only_enabled: bool = False) -> list[AutobuyJob]:
        q = "SELECT id, plan_id, service_name, plan_label, interval_min, enabled, bought_count, last_run_at, last_status FROM autobuy_jobs"
        if only_enabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY id DESC"
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(q) as cur:
                rows = await cur.fetchall()
        return [
            AutobuyJob(
                id=r[0], plan_id=r[1], service_name=r[2], plan_label=r[3],
                interval_min=r[4], enabled=bool(r[5]), bought_count=r[6],
                last_run_at=r[7], last_status=r[8],
            )
            for r in rows
        ]

    async def get_job(self, job_id: int) -> AutobuyJob | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id, plan_id, service_name, plan_label, interval_min, enabled, bought_count, last_run_at, last_status FROM autobuy_jobs WHERE id = ?",
                (job_id,),
            ) as cur:
                r = await cur.fetchone()
        if not r:
            return None
        return AutobuyJob(
            id=r[0], plan_id=r[1], service_name=r[2], plan_label=r[3],
            interval_min=r[4], enabled=bool(r[5]), bought_count=r[6],
            last_run_at=r[7], last_status=r[8],
        )

    async def set_enabled(self, job_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE autobuy_jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
            await db.commit()

    async def set_interval(self, job_id: int, interval_min: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE autobuy_jobs SET interval_min = ? WHERE id = ?", (interval_min, job_id))
            await db.commit()

    async def delete_job(self, job_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM autobuy_jobs WHERE id = ?", (job_id,))
            await db.commit()

    async def record_run(self, job_id: int, bought: int, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE autobuy_jobs SET bought_count = bought_count + ?, last_run_at = datetime('now'), last_status = ? WHERE id = ?",
                (bought, status, job_id),
            )
            await db.commit()
