from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db import DB, AutobuyJob
from gotsms_api import GotSmsClient, GotSmsError, NoNumbersAvailable, InsufficientFunds

log = logging.getLogger("autobuy")

# Лестница батчей под лимит API (30 запросов/мин на всё): держим полный
# батч ступень, на недоборе спускаемся 25 → 10 → 1. Темп держит rate-limiter
# в клиенте (GotSmsClient), так что 429 ловить не будем.
BATCH_LADDER = [25, 10, 1]

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
            asyncio.create_task(self._tick(job.id))

    def _job_id(self, job_id: int) -> str:
        return f"autobuy:{job_id}"

    def _schedule(self, job: AutobuyJob) -> None:
        sid = self._job_id(job.id)
        if self.scheduler.get_job(sid):
            self.scheduler.remove_job(sid)
        self.scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(seconds=max(10, job.interval_sec)),
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

    async def set_interval(self, job_id: int, interval_sec: int) -> None:
        await self.db.set_interval(job_id, interval_sec)
        job = await self.db.get_job(job_id)
        if job and job.enabled:
            self._schedule(job)

    async def set_limit(self, job_id: int, buy_limit: int) -> None:
        await self.db.set_limit(job_id, buy_limit)
        # лимит читается из БД на каждом тике — перепланировать не нужно

    async def remove(self, job_id: int) -> None:
        self._unschedule(job_id)
        await self.db.delete_job(job_id)

    async def _buy_one(self, plan_id: str) -> tuple[str, object | None]:
        """Одна покупка. Возвращает ('ok', rent) | ('no_numbers'|'insufficient_funds'|f'err:{code}', None)."""
        try:
            rent = await self.api.create_rent(plan_id)
            return ("ok", rent)
        except NoNumbersAvailable:
            return ("no_numbers", None)
        except InsufficientFunds:
            return ("insufficient_funds", None)
        except GotSmsError as e:
            log.warning("buy failed: %s", e)
            return (f"err:{e.status}", None)

    async def _tick(self, job_id: int) -> None:
        async with self._lock:  # serialize all autobuy ticks to avoid race on balance
            job = await self.db.get_job(job_id)
            if not job or not job.enabled:
                return

            # лимит уже выбран — гасим задание без лишних запросов к API
            if job.buy_limit and job.bought_count >= job.buy_limit:
                await self.disable(job.id)
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

            # backfill service_id for jobs created before the column existed
            if not job.service_id:
                await self.db.record_run(job.id, 0, "missing_service_id")
                await self.disable(job.id)
                await self.notify(
                    f"⛔ Автобай <b>{job.service_name}</b> создан в старой версии "
                    f"бота (нет service_id). Удали и пересоздай."
                )
                return

            # refresh price each tick (bypass cache, fetch only this service's plans)
            try:
                plans = await self.api.plans_all(service_id=job.service_id, per_page=100, use_cache=False)
                target = next((p for p in plans if p.id == job.plan_id), None)
                price = target.price if target else 0.0
            except GotSmsError as e:
                log.warning("plans fetch failed: %s", e)
                price = 0.0

            if price <= 0:
                await self.db.record_run(job.id, 0, "no_price")
                log.warning("no price for job=%s plan=%s service=%s", job.id, job.plan_id, job.service_id)
                return

            limit = job.buy_limit  # 0 = без лимита
            already = job.bought_count
            rung = 0  # ступень в BATCH_LADDER: 100 → 50 → 25 → 1

            # батчевый выкуп с градацией: полный батч держим ступень, на недоборе — ниже
            while balance >= price and (limit == 0 or already + len(bought) < limit):
                batch = BATCH_LADDER[rung]
                affordable = int(balance // price)              # сколько потянет баланс
                room = (limit - already - len(bought)) if limit else affordable  # сколько до лимита
                n = min(batch, affordable, room)
                if n <= 0:
                    if affordable <= 0:
                        status = "insufficient_funds"
                    break

                results = await asyncio.gather(*[self._buy_one(job.plan_id) for _ in range(n)])
                kinds = [s for (s, _) in results]
                got = [r for (s, r) in results if s == "ok" and r is not None]

                for rent in got:
                    bought.append(rent.phone)
                if got:
                    sample = "\n".join(f"<code>{r.phone}</code>" for r in got[:50])
                    await self.notify(f"✅ Куплено {len(got)} ({job.service_name}):\n{sample}")

                if not got:
                    # ничего не купили на этой ступени → пул пуст / лимит провайдера / 429 / ошибка
                    if "insufficient_funds" in kinds:
                        status = "insufficient_funds"
                    elif "no_numbers" in kinds:
                        status = "no_numbers"
                    else:
                        errs = [s for s in kinds if s.startswith("err:")]
                        status = errs[0] if errs else "no_numbers"
                    break

                # купили меньше, чем пытались → пул сдувается, спускаемся ступенью ниже
                if len(got) < n and rung < len(BATCH_LADDER) - 1:
                    rung += 1

                try:
                    balance = await self.api.balance()
                except GotSmsError:
                    break

            await self.db.record_run(job.id, len(bought), status)
            total = already + len(bought)
            if limit and total >= limit:
                await self.disable(job.id)
                await self.notify(
                    f"🎯 Автобай <b>{job.service_name}</b>: лимит {limit} достигнут — остановлен. "
                    f"Куплено всего: {total}."
                )
            elif bought:
                await self.notify(
                    f"🤖 Автобай <b>{job.service_name}</b>: куплено {len(bought)} шт. за тик. "
                    f"Остаток: {balance:.2f}"
                )
