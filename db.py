from __future__ import annotations

import os
from dataclasses import dataclass

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS autobuy_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    service_id TEXT,
    service_name TEXT NOT NULL,
    plan_label TEXT NOT NULL,
    interval_sec INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    bought_count INTEGER NOT NULL DEFAULT 0,
    buy_limit INTEGER NOT NULL DEFAULT 0,
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
    service_id: str | None
    service_name: str
    plan_label: str
    interval_sec: int
    enabled: bool
    bought_count: int
    buy_limit: int
    last_run_at: str | None
    last_status: str | None


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            # additive migration: add service_id column to pre-existing tables
            async with db.execute("PRAGMA table_info(autobuy_jobs)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            if "service_id" not in cols:
                await db.execute("ALTER TABLE autobuy_jobs ADD COLUMN service_id TEXT")
            # interval_min (minutes) → interval_sec (seconds): add column, seed from old value
            if "interval_sec" not in cols:
                await db.execute("ALTER TABLE autobuy_jobs ADD COLUMN interval_sec INTEGER NOT NULL DEFAULT 30")
                if "interval_min" in cols:
                    await db.execute("UPDATE autobuy_jobs SET interval_sec = interval_min * 60")
            if "buy_limit" not in cols:
                await db.execute("ALTER TABLE autobuy_jobs ADD COLUMN buy_limit INTEGER NOT NULL DEFAULT 0")
            await db.commit()

    async def add_job(self, plan_id: str, service_id: str, service_name: str, plan_label: str, interval_sec: int, buy_limit: int = 0) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO autobuy_jobs (plan_id, service_id, service_name, plan_label, interval_sec, buy_limit) VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, service_id, service_name, plan_label, interval_sec, buy_limit),
            )
            await db.commit()
            return cur.lastrowid

    async def set_service_id(self, job_id: int, service_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE autobuy_jobs SET service_id = ? WHERE id = ?", (service_id, job_id))
            await db.commit()

    async def list_jobs(self, only_enabled: bool = False) -> list[AutobuyJob]:
        q = "SELECT id, plan_id, service_id, service_name, plan_label, interval_sec, enabled, bought_count, last_run_at, last_status, buy_limit FROM autobuy_jobs"
        if only_enabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY id DESC"
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(q) as cur:
                rows = await cur.fetchall()
        return [
            AutobuyJob(
                id=r[0], plan_id=r[1], service_id=r[2], service_name=r[3], plan_label=r[4],
                interval_sec=r[5], enabled=bool(r[6]), bought_count=r[7],
                last_run_at=r[8], last_status=r[9], buy_limit=r[10],
            )
            for r in rows
        ]

    async def get_job(self, job_id: int) -> AutobuyJob | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT id, plan_id, service_id, service_name, plan_label, interval_sec, enabled, bought_count, last_run_at, last_status, buy_limit FROM autobuy_jobs WHERE id = ?",
                (job_id,),
            ) as cur:
                r = await cur.fetchone()
        if not r:
            return None
        return AutobuyJob(
            id=r[0], plan_id=r[1], service_id=r[2], service_name=r[3], plan_label=r[4],
            interval_sec=r[5], enabled=bool(r[6]), bought_count=r[7],
            last_run_at=r[8], last_status=r[9], buy_limit=r[10],
        )

    async def set_enabled(self, job_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE autobuy_jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
            await db.commit()

    async def set_interval(self, job_id: int, interval_sec: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE autobuy_jobs SET interval_sec = ? WHERE id = ?", (interval_sec, job_id))
            await db.commit()

    async def set_limit(self, job_id: int, buy_limit: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE autobuy_jobs SET buy_limit = ? WHERE id = ?", (buy_limit, job_id))
            await db.commit()

    async def delete_job(self, job_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM autobuy_jobs WHERE id = ?", (job_id,))
            await db.commit()

    async def get_setting(self, key: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                r = await cur.fetchone()
        return r[0] if r else None

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def record_run(self, job_id: int, bought: int, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE autobuy_jobs SET bought_count = bought_count + ?, last_run_at = datetime('now'), last_status = ? WHERE id = ?",
                (bought, status, job_id),
            )
            await db.commit()
