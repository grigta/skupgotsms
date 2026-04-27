from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db import DB, AutobuyJob
from gotsms_api import GotSmsClient, GotSmsError, NoNumbersAvailable, InsufficientFunds

log = logging.getLogger("autobuy")

# Notify callback: (text) -> awaitable. Set by main.
NotifyFn = Callable[[str], Awaitable[None]]


class AutobuyManager:
    def __init__(self, db: DB, api: GotSmsClient, notify: NotifyFn):
        self.db = db
        self.api = api
        self.notify = notify
        self.scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self.scheduler.start()

    async def restore(self) -> None:
        for job in await self.db.list_jobs(only_enabled=True):
            self._schedule(job)

    def _job_id(self, job_id: int) -> str:
        return f"autobuy:{job_id}"

    def _schedule(self, job: AutobuyJob) -> None:
        sid = self._job_id(job.id)
        if self.scheduler.get_job(sid):
            self.scheduler.remove_job(sid)
        self.scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(minutes=job.interval_min),
            id=sid,
            args=[job.id],
            next_run_time=None,
            max_instances=1,
            coalesce=True,
        )

    def _unschedule(self, job_id: int) -> None:
        sid = self._job_id(job_id)
        if self.scheduler.get_job(sid):
            self.scheduler.remove_job(sid)

    async def enable(self, job_id: int) -> None:
        await self.db.set_enabled(job_id, True)
        job = await self.db.get_job(job_id)
        if job:
            self._schedule(job)
            # fire immediately, then on interval
            asyncio.create_task(self._tick(job_id))

    async def disable(self, job_id: int) -> None:
        await self.db.set_enabled(job_id, False)
        self._unschedule(job_id)

    async def set_interval(self, job_id: int, interval_min: int) -> None:
        await self.db.set_interval(job_id, interval_min)
        job = await self.db.get_job(job_id)
        if job and job.enabled:
            self._schedule(job)

    async def remove(self, job_id: int) -> None:
        self._unschedule(job_id)
        await self.db.delete_job(job_id)

    async def _tick(self, job_id: int) -> None:
        async with self._lock:  # serialize all autobuy ticks to avoid race on balance
            job = await self.db.get_job(job_id)
            if not job or not job.enabled:
                return

            log.info("autobuy tick job=%s plan=%s", job.id, job.plan_id)
            bought: list[str] = []
            status = "ok"

            try:
                balance = await self.api.balance()
            except GotSmsError as e:
                await self.db.record_run(job.id, 0, f"balance_err:{e.status}")
                await self.notify(f"⚠️ Автобай <b>{job.service_name}</b>: ошибка баланса {e.status}")
                if e.status in (401, 403):
                    await self.disable(job.id)
                    await self.notify(f"⛔ Автобай <b>{job.service_name}</b> остановлен (auth error)")
                return

            # refresh price each tick
            try:
                plans, _ = await self.api.plans()
                target = next((p for p in plans if p.id == job.plan_id), None)
                price = target.price if target else 0.0
            except GotSmsError:
                price = 0.0

            if price <= 0:
                await self.db.record_run(job.id, 0, "no_price")
                return

            # greedy buy while balance allows
            while balance >= price:
                try:
                    rent = await self.api.create_rent(job.plan_id)
                except NoNumbersAvailable:
                    status = "no_numbers"
                    break
                except InsufficientFunds:
                    status = "insufficient_funds"
                    break
                except GotSmsError as e:
                    status = f"err:{e.status}"
                    log.warning("buy failed: %s", e)
                    break

                bought.append(rent.phone)
                await self.notify(
                    f"✅ Куплен номер <code>{rent.phone}</code>\n"
                    f"Сервис: <b>{rent.service_name}</b>\n"
                    f"Цена: {rent.price}\n"
                    f"Активен до: {rent.active_till or '—'}"
                )
                # refresh balance from server (price could have changed too, but rare per tick)
                try:
                    balance = await self.api.balance()
                except GotSmsError:
                    break

            await self.db.record_run(job.id, len(bought), status)
            if bought:
                await self.notify(
                    f"🤖 Автобай <b>{job.service_name}</b>: куплено {len(bought)} шт. за тик. "
                    f"Остаток: {balance:.2f}"
                )
