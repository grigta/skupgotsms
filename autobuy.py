from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db import DB, AutobuyJob
from gotsms_api import GotSmsClient, GotSmsError, NoNumbersAvailable, InsufficientFunds
from gotsms_lk import LkClient, LkAuthError, LkError

log = logging.getLogger("autobuy")

# Лестница батчей под лимит API (30 запросов/мин на всё): держим полный
# батч ступень, на недоборе спускаемся 25 → 10 → 1. Темп держит rate-limiter
# в клиенте (GotSmsClient), так что 429 ловить не будем.
BATCH_LADDER = [25, 10, 1]

# Notify callback: (text) -> awaitable. Set by main.
NotifyFn = Callable[[str], Awaitable[None]]


class AutobuyManager:
    def __init__(self, db: DB, api: GotSmsClient, notify: NotifyFn, lk: LkClient | None = None):
        self.db = db
        self.api = api
        self.notify = notify
        self.lk = lk  # ЛК-клиент для bulk-покупки (None = только публичный API)
        self.scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}  # hunter-циклы по job_id (LK-режим)

    def start(self) -> None:
        self.scheduler.start()

    async def restore(self) -> None:
        for job in await self.db.list_jobs(only_enabled=True):
            self._start_job(job)

    async def restart_jobs(self) -> None:
        """Перезапустить все включённые задания на текущем механизме
        (hunter-loop если есть ЛК, иначе scheduler). Вызывается после
        добавления/удаления первого ЛК-аккаунта через /lk."""
        for job in await self.db.list_jobs(only_enabled=True):
            self._stop_job(job.id)
            self._start_job(job)

    async def _autoswitch(self) -> bool:
        """Если включено автопереключение и активный аккаунт пуст — перейти на
        следующий аккаунт с балансом (cookie + API-токен). True если переключились."""
        if (await self.db.get_setting("lk_autoswitch")) != "1":
            return False
        if not self.lk:
            return False
        accts = await self.db.lk_accounts()
        if len(accts) < 2:
            return False
        cur = await self.db.lk_active_idx()
        from gotsms_lk import LkClient
        for off in range(1, len(accts) + 1):
            i = (cur + off) % len(accts)
            if i == cur:
                continue
            a = accts[i]
            tmp = LkClient(a["session"], a["xsrf"], self.lk._ua, self.lk.base)  # noqa: SLF001
            try:
                bal = await tmp.balance()
            except Exception:
                bal = None
            finally:
                await tmp.aclose()
            if bal and bal > 0:
                await self.db.lk_set_active(i)
                await self.lk.update_cookies(a["session"], a["xsrf"])
                self.api.set_token(a.get("api_token") or "")
                await self.notify(f"🔄 Автопереключение на аккаунт <b>{a.get('label')}</b> (баланс ${bal:.2f})")
                return True
        return False

    # ───────── управление job: LK → hunter-loop, иначе scheduler ─────────
    def _start_job(self, job: AutobuyJob) -> None:
        if self.lk:
            self._start_loop(job.id)
        else:
            self._schedule(job)
            asyncio.create_task(self._tick(job.id))

    def _stop_job(self, job_id: int) -> None:
        self._stop_loop(job_id)
        self._unschedule(job_id)

    def _start_loop(self, job_id: int) -> None:
        old = self._tasks.get(job_id)
        if old and not old.done():
            old.cancel()
        self._tasks[job_id] = asyncio.create_task(self._hunt_loop(job_id))

    def _stop_loop(self, job_id: int) -> None:
        t = self._tasks.pop(job_id, None)
        if t and not t.done():
            t.cancel()

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
            self._start_job(job)

    async def disable(self, job_id: int) -> None:
        await self.db.set_enabled(job_id, False)
        self._stop_job(job_id)

    async def set_interval(self, job_id: int, interval_sec: int) -> None:
        await self.db.set_interval(job_id, interval_sec)
        job = await self.db.get_job(job_id)
        if job and job.enabled and not self.lk:
            self._schedule(job)  # hunter-loop читает интервал из БД сам

    async def set_limit(self, job_id: int, buy_limit: int) -> None:
        await self.db.set_limit(job_id, buy_limit)
        # лимит читается из БД на каждом круге — перезапуск не нужен

    async def remove(self, job_id: int) -> None:
        self._stop_job(job_id)
        await self.db.delete_job(job_id)

    async def _hunt_loop(self, job_id: int) -> None:
        """Near-realtime охотник (LK-режим): непрерывно probe'ит наличие (~0.8с,
        не лимитируется) и мгновенно выкупает пачкой, как только номера появятся.
        balance/price тянем из API редко (раз в 25с) — чтобы не упереться в 30/мин."""
        loop = asyncio.get_event_loop()
        price = 0.0
        balance = 0.0
        last_meta = -1e9
        try:
            while True:
                job = await self.db.get_job(job_id)
                if not job or not job.enabled:
                    return
                if job.buy_limit and job.bought_count >= job.buy_limit:
                    await self.db.set_enabled(job.id, False)
                    self._tasks.pop(job_id, None)
                    await self.notify(
                        f"🎯 Автобай <b>{job.service_name}</b>: лимит {job.buy_limit} достигнут — остановлен."
                    )
                    return
                pause = max(1, min(job.interval_sec, 5))  # частота poll на пустом пуле

                # price + balance из API не чаще раза в 25с (бережём лимит 30/мин)
                now = loop.time()
                if price <= 0 or now - last_meta > 25:
                    try:
                        balance = await self.api.balance()
                        plans = await self.api.plans_all(service_id=job.service_id, per_page=100, use_cache=True)
                        t = next((p for p in plans if p.id == job.plan_id), None)
                        price = t.price if t else 0.0
                        last_meta = now
                    except GotSmsError as e:
                        if getattr(e, "status", 0) in (401, 403):
                            await self.db.set_enabled(job.id, False)
                            self._tasks.pop(job_id, None)
                            await self.notify(f"⛔ Автобай <b>{job.service_name}</b> остановлен (auth error)")
                            return
                        await asyncio.sleep(pause)
                        continue

                if price <= 0:
                    await asyncio.sleep(pause)
                    continue
                if balance < price:
                    # активный аккаунт пуст — пробуем автопереключение на другой
                    if await self._autoswitch():
                        price = 0.0
                        last_meta = -1e9  # форс refetch баланса/цены нового аккаунта
                        continue
                    await asyncio.sleep(pause)
                    continue

                # дешёвая проверка наличия через ЛК (openModal ~0.8с, не лимит)
                try:
                    modal, avail = await self.lk.probe(job.plan_id)
                except LkAuthError:
                    await self.db.set_enabled(job.id, False)
                    self._tasks.pop(job_id, None)
                    await self.notify(
                        f"⛔ Автобай <b>{job.service_name}</b>: ЛК-сессия протухла — обнови через /lk"
                    )
                    return
                except LkError:
                    await asyncio.sleep(pause)
                    continue

                if not modal or avail <= 0:
                    await asyncio.sleep(pause)  # пул пуст — ждём и снова probe
                    continue

                # есть номера! выкупаем пачкой одним rent
                room = (job.buy_limit - job.bought_count) if job.buy_limit else avail
                n = min(25, avail, int(balance // price), room)
                if n <= 0:
                    await asyncio.sleep(pause)
                    continue
                try:
                    cnt, st = await self.lk.rent(modal, n)
                except (LkAuthError, LkError) as e:
                    log.warning("hunt rent job=%s: %s", job_id, e)
                    await asyncio.sleep(pause)
                    continue

                if cnt > 0:
                    balance -= price * cnt
                    await self.db.record_run(job.id, cnt, "ok")
                    await self.notify(f"✅ Куплено {cnt} ({job.service_name}). Остаток: {balance:.2f}")
                    # пул может ещё держать — сразу следующий круг без паузы
                    continue
                else:
                    # между probe и rent номера разобрали — короткая пауза
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("hunt_loop job=%s crashed: %s", job_id, e)

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

            if self.lk:
                # ── bulk-выкуп пачками по 25 через ЛК (Livewire, без лимита 30/мин) ──
                while balance >= price and (limit == 0 or already + len(bought) < limit):
                    room = (limit - already - len(bought)) if limit else 25
                    n = min(25, int(balance // price), room)
                    if n <= 0:
                        if int(balance // price) <= 0:
                            status = "insufficient_funds"
                        break
                    try:
                        cnt, st = await self.lk.buy(job.plan_id, n)
                    except LkAuthError:
                        status = "lk_auth"
                        await self.notify(
                            f"⛔ Автобай <b>{job.service_name}</b>: ЛК-сессия протухла — "
                            f"обнови cookie (bulk-выкуп остановлен)"
                        )
                        break
                    except LkError as e:
                        status = "lk_err"
                        log.warning("lk buy failed: %s", e)
                        break
                    if cnt <= 0:
                        status = st  # no_numbers / insufficient_funds / err
                        break
                    bought.extend(["lk"] * cnt)
                    balance -= price * cnt
                    await self.notify(
                        f"✅ Куплено {cnt} пачкой ({job.service_name}). Остаток: {balance:.2f}"
                    )
            else:
                # ── fallback: публичный API по 1, батчами с лесенкой под лимит 30/мин ──
                rung = 0          # ступень в BATCH_LADDER (25 → 10 → 1)
                probed = False    # первый раунд — разведка 1 номером
                while balance >= price and (limit == 0 or already + len(bought) < limit):
                    batch = 1 if not probed else BATCH_LADDER[rung]
                    affordable = int(balance // price)
                    room = (limit - already - len(bought)) if limit else affordable
                    budget = self.api.rate_remaining()
                    n = min(batch, affordable, room, max(1, budget))
                    if n <= 0:
                        if affordable <= 0:
                            status = "insufficient_funds"
                        break

                    results = await asyncio.gather(*[self._buy_one(job.plan_id) for _ in range(n)])
                    kinds = [s for (s, _) in results]
                    got = [r for (s, r) in results if s == "ok" and r is not None]

                    for rent in got:
                        bought.append(rent.phone)
                    balance -= price * len(got)
                    if got:
                        sample = "\n".join(f"<code>{r.phone}</code>" for r in got[:50])
                        await self.notify(f"✅ Куплено {len(got)} ({job.service_name}):\n{sample}")

                    if not got:
                        if "insufficient_funds" in kinds:
                            status = "insufficient_funds"
                        elif "no_numbers" in kinds:
                            status = "no_numbers"
                        else:
                            errs = [s for s in kinds if s.startswith("err:")]
                            status = errs[0] if errs else "no_numbers"
                        break

                    if not probed:
                        probed = True
                    elif len(got) < n and rung < len(BATCH_LADDER) - 1:
                        rung += 1

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
