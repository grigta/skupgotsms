from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from db import DB, AutobuyJob
from gotsms_api import GotSmsClient, GotSmsError, NoNumbersAvailable, InsufficientFunds
from gotsms_lk import MAX_PER_RENT, LkClient, LkPool, LkAuthError, LkError

log = logging.getLogger("autobuy")

# Лестница батчей под лимит API (30 запросов/мин на всё): держим полный
# батч ступень, на недоборе спускаемся 25 → 10 → 1. Темп держит rate-limiter
# в клиенте (GotSmsClient), так что 429 ловить не будем.
BATCH_LADDER = [25, 10, 1]

# Раз в BLIND_EVERY секунд hunter пробует rent даже если probe показал «пусто» —
# страховка от заниженного area-code count (probe врёт для части сервисов).
BLIND_EVERY = 30.0

# Как часто охотник отмечается «жив» в БД (карточка задания показывает это в
# «Последний тик»). Реже — чтобы не долбить SQLite на интервале в 1-2 секунды.
BEAT_EVERY = 30.0

# Конвейер проб: сколько дорожек и с каким сдвигом. Одна дорожка = прежнее
# поведение. Больше дорожек = раньше детект, но во столько же раз больше
# запросов к gotsms (перед ним Cloudflare — не задирать без нужды).
# Переопределяется настройкой probe_lanes в БД.
PROBE_LANES = 3
PROBE_STAGGER = 0.4

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
        self._beats: dict[int, float] = {}  # когда последний раз отметились «живы»
        self._pool: LkPool | None = None  # параллельный выкуп всеми аккаунтами
        self._pool_sig: str = ""  # состав аккаунтов, под который собран пул
        self._pool_extra: list[LkClient] = []  # клиенты неактивных аккаунтов

    def start(self) -> None:
        self.scheduler.start()
        # БЕЗ next_run_time=None: в APScheduler это способ добавить задачу
        # ПРИОСТАНОВЛЕННОЙ, из-за чего watchdog не запускался ни разу и умершие
        # охотники никто не поднимал.
        self.scheduler.add_job(
            self._watchdog, "interval", seconds=120, id="hunt_watchdog",
            max_instances=1, coalesce=True, replace_existing=True,
        )

    async def _ensure_pool(self) -> LkPool | None:
        """Пул по всем ЛК-аккаунтам (None — если аккаунт один, пул не нужен).

        Сервер сериализует покупки внутри аккаунта (lock на балансе), поэтому
        скорость растёт только за счёт РАЗНЫХ аккаунтов. Активный переиспользует
        self.lk — не плодим лишние httpx-клиенты и держим keep-alive."""
        if not self.lk:
            return None
        accts = await self.db.lk_accounts()
        if len(accts) < 2:
            return None
        active = await self.db.lk_active_idx()
        if not (0 <= active < len(accts)):
            active = 0
        sig = "#%d|" % active + "|".join(
            "%s:%s:%s" % (a.get("label"), (a.get("session") or "")[:16], a.get("proxy") or "")
            for a in accts
        )
        if sig == self._pool_sig and self._pool is not None:
            return self._pool
        for c in self._pool_extra:  # состав сменился — старые клиенты закрываем
            try:
                await c.aclose()
            except Exception:
                pass
        self._pool_extra = []
        clients: list[LkClient] = []
        for i, a in enumerate(accts):
            if i == active:
                clients.append(self.lk)
                continue
            c = LkClient(a["session"], a["xsrf"], self.lk._ua, self.lk.base,  # noqa: SLF001
                         proxy=a.get("proxy"))
            self._pool_extra.append(c)
            clients.append(c)
        self._pool = LkPool.from_clients(clients)
        self._pool_sig = sig
        log.info("ЛК-пул собран: %d аккаунтов (%s)", len(clients),
                 ", ".join(str(a.get("label")) for a in accts))
        return self._pool

    async def _probe_lanes(self) -> int:
        """Сколько дорожек конвейера. Настройка probe_lanes в БД (1 = выключить)."""
        raw = await self.db.get_setting("probe_lanes")
        try:
            return max(1, min(int(raw), 8)) if raw else PROBE_LANES
        except (TypeError, ValueError):
            return PROBE_LANES

    async def _probe_burst(self, plan_id: str, lanes: int, stagger: float):
        """Конвейер: `lanes` проб со сдвигом `stagger`, вместо одной по очереди.

        Смысл — не пропускная способность, а РАННИЙ детект. Одиночная проба
        видит завоз в среднем через cadence/2 (~0.65с при такте 1.3с); дорожки
        со сдвигом 0.4с сокращают это до ~0.2с. За эти доли секунды и идёт
        гонка с другими ботами за небольшой пул.

        Возвращает первую пробу, увидевшую сток; иначе — любую с модалкой."""
        if lanes <= 1:
            return await self.lk.probe(plan_id)

        async def lane(i: int):
            if i:
                await asyncio.sleep(i * stagger)
            return await self.lk.probe(plan_id)

        tasks = [asyncio.create_task(lane(i)) for i in range(lanes)]
        best = (None, 0, 0, "", 0.0)
        errs: list[Exception] = []
        any_ok = False  # хотя бы одна дорожка вернула ответ без исключения
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    res = await fut
                except (LkAuthError, LkError) as e:
                    errs.append(e)
                    continue
                any_ok = True
                if res[0] is not None and res[1] > 0:
                    return res  # сток найден — остальные дорожки не нужны
                if best[0] is None and res[0] is not None:
                    best = res
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            # даём отменённым задачам свернуться, чтобы не сыпать warning'ами
            await asyncio.gather(*tasks, return_exceptions=True)
        # Бросаем ошибку только если ВСЕ дорожки упали: если хоть одна вернула
        # ответ без исключения — сессия жива и ложный LkAuthError недопустим
        # (он остановит охоту и выключит задание, хотя протухания не было).
        if not any_ok and errs:
            auth_err = next((e for e in errs if isinstance(e, LkAuthError)), None)
            raise (auth_err if auth_err else errs[0])
        return best

    @staticmethod
    async def _nap(loop, cycle_start: float, interval_sec: int) -> None:
        """Доспать остаток такта. probe уже съел часть интервала — раньше мы
        спали ПОВЕРХ него, из-за чего «интервал 2с» на деле давал ~2.3с, а
        меньше секунды выставить было нельзя вовсе."""
        left = min(max(interval_sec, 0), 5) - (loop.time() - cycle_start)
        if left > 0:
            await asyncio.sleep(left)

    async def _hunt_balance(self) -> float | None:
        """Баланс активного аккаунта. Сначала через ЛК-куку (Livewire) — она жива
        даже когда API-токен протух; иначе fallback на публичный API. Это снимает
        зависимость автобая от API-токена, который постоянно дохнет."""
        if self.lk:
            try:
                b = await self.lk.balance()
                if b is not None:
                    return b
            except Exception as e:
                log.warning("hunt balance via LK: %s", e)
        try:
            return await self.api.balance()
        except GotSmsError as e:
            log.warning("hunt balance via API: %s", e)
            return None

    async def _beat(self, job_id: int, now: float, status: str) -> None:
        """Отметить, что охотник жив (не покупка). Иначе задание с нулём покупок
        неотличимо от задания, чей цикл давно умер."""
        if now - self._beats.get(job_id, -1e9) < BEAT_EVERY:
            return
        self._beats[job_id] = now
        try:
            await self.db.record_tick(job_id, status)
        except Exception as e:  # пульс не должен ронять охоту
            log.warning("beat job=%s: %s", job_id, e)

    async def _watchdog(self) -> None:
        """Перезапустить охотников для включённых заданий, чьи задачи умерли."""
        if not self.lk:
            return  # без ЛК работает scheduler-механизм
        for job in await self.db.list_jobs(only_enabled=True):
            t = self._tasks.get(job.id)
            if t is None or t.done():
                log.warning("watchdog: перезапуск hunt job=%s", job.id)
                self._start_loop(job.id)

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

    async def _autoswitch(self, price: float = 0.0) -> bool:
        """Если включено автопереключение и активный аккаунт пуст — перейти на
        следующий аккаунт с балансом (cookie + API-токен). True если переключились.
        price > 0: переключаемся только на аккаунт, где баланс >= price, иначе
        охотник зациклится между аккаунтами с ненулевым, но недостаточным балансом."""
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
            tmp = LkClient(a["session"], a["xsrf"], self.lk._ua, self.lk.base, proxy=a.get("proxy"))  # noqa: SLF001
            try:
                bal = await tmp.balance()
            except Exception:
                bal = None
            finally:
                await tmp.aclose()
            if bal and bal >= max(price, 0):
                await self.db.lk_set_active(i)
                await self.lk.update_cookies(a["session"], a["xsrf"])
                if a.get("api_token"):
                    self.api.set_token(a["api_token"])
                await self.notify(f"🔄 Автопереключение на аккаунт <b>{a.get('label')}</b> (баланс ${bal:.2f})")
                return True
        return False

    # ───────── управление job: LK → hunter-loop, иначе scheduler ─────────
    def _start_job(self, job: AutobuyJob) -> None:
        if self.lk:
            self._start_loop(job.id)
        else:
            # Молчаливая деградация — худший вариант: задание горит «включено», а
            # быстрого выкупа нет и пользователь об этом не знает. Говорим вслух.
            log.warning("job=%s запущен БЕЗ ЛК — медленный режим (добавь аккаунт через /lk)", job.id)
            asyncio.create_task(self.notify(
                f"⚠️ Автобай <b>{job.service_name}</b> работает в медленном режиме: "
                f"нет ЛК-аккаунта. Добавь его через /lk — иначе быстрый выкуп недоступен."
            ))
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
        last_blind = -1e9  # когда последний раз пробовали rent «вслепую»
        lanes = PROBE_LANES  # обновляется вместе с price/balance
        try:
            while True:
                cycle_start = loop.time()  # такт считаем ОТ начала, не поверх probe
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

                # Баланс — через ЛК-куку раз в 25с. НЕ через API-токен: он
                # постоянно дохнет и раньше 401 молча выключал задание. Кука
                # живёт, пока аккаунт активен в /lk.
                now = loop.time()
                if now - last_meta > 25:
                    b = await self._hunt_balance()
                    if b is not None:
                        balance = b
                    last_meta = now
                    lanes = await self._probe_lanes()

                # probe (openModal ~1.3с, не лимит) даёт И наличие, И цену — цена
                # есть в модалке даже при пустом пуле, поэтому API-токен не нужен.
                # Конвейер в несколько дорожек — чтобы заметить завоз раньше.
                try:
                    modal, avail, maxq, code, mprice = await self._probe_burst(
                        job.plan_id, lanes, PROBE_STAGGER)
                except LkAuthError:
                    await self.db.set_enabled(job.id, False)
                    self._tasks.pop(job_id, None)
                    await self.notify(
                        f"⛔ Автобай <b>{job.service_name}</b>: ЛК-сессия протухла — обнови через /lk"
                    )
                    return
                except LkError:
                    await self._nap(loop, cycle_start, job.interval_sec)
                    continue

                if mprice > 0:
                    price = mprice  # свежая цена из модалки, без API
                if price <= 0:
                    await self._beat(job_id, loop.time(), "нет цены")
                    await self._nap(loop, cycle_start, job.interval_sec)
                    continue
                if balance < price:
                    # активный аккаунт пуст — пробуем автопереключение на другой
                    if await self._autoswitch(price):
                        last_meta = -1e9  # форс refetch баланса нового аккаунта
                        continue
                    await self._beat(job_id, loop.time(), "нет баланса")
                    await self._nap(loop, cycle_start, job.interval_sec)
                    continue

                now2 = loop.time()
                # gate решает «пробовать ли»: probe видит сток ЛИБО давно не пробовали
                # вслепую (probe бывает врёт — area-code count занижен).
                attempt = (modal is not None) and (avail > 0 or (now2 - last_blind) >= BLIND_EVERY)
                if not attempt:
                    await self._beat(job_id, now2, "пул пуст" if modal is not None else "модалка не отдалась")
                    await self._nap(loop, cycle_start, job.interval_sec)
                    continue
                if avail <= 0:
                    last_blind = now2  # засекаем слепую попытку

                # Количество зажимаем в maxQuantity: это потолок сервера на один
                # rent, и заказ сверх него отвергается целиком (avail по area-кодам
                # бывает в сотнях, а взять за раз можно 1). Раньше этого не было —
                # бот бесконечно просил 25 и получал отказ при полном пуле.
                cap = maxq if maxq > 0 else MAX_PER_RENT
                room = (job.buy_limit - job.bought_count) if job.buy_limit else cap
                n = min(cap, MAX_PER_RENT, int(balance // price), room)
                if n <= 0:
                    await self._beat(job_id, now2, "нет места/баланса")
                    await self._nap(loop, cycle_start, job.interval_sec)
                    continue
                try:
                    cnt, st = await self.lk.rent(modal, n, code)
                except LkAuthError:
                    await self.db.set_enabled(job.id, False)
                    self._tasks.pop(job_id, None)
                    await self.notify(
                        f"⛔ Автобай <b>{job.service_name}</b>: ЛК-сессия протухла (rent) — обнови через /lk"
                    )
                    return
                except LkError as e:
                    log.warning("hunt rent job=%s: %s", job_id, e)
                    await self._nap(loop, cycle_start, job.interval_sec)
                    continue

                if cnt > 0:
                    balance -= price * cnt
                    # Записываем купленное ДО await-операций пула: CancelledError
                    # (BaseException, не Exception) не ловится guard'ами ниже и
                    # улетает в except CancelledError: return — если record_run
                    # стоял после пула, покупки уходили без учёта в bought_count.
                    try:
                        await self.db.record_run(job.id, cnt, "ok")
                    except Exception as db_err:
                        log.warning("hunt record_run job=%s cnt=%d: %s", job_id, cnt, db_err)
                    # Сток есть — добираем остаток ПАРАЛЛЕЛЬНО остальными аккаунтами.
                    # Внутри одного аккаунта сервер сериализует покупки (lock на
                    # балансе), поэтому ускорение даёт только фан-аут по разным.
                    extra = 0
                    try:
                        pool = await self._ensure_pool()
                    except Exception as e:
                        log.warning("ensure_pool job=%s: %s", job_id, e)
                        pool = None
                    if pool is not None:
                        left = (job.buy_limit - job.bought_count - cnt) if job.buy_limit else MAX_PER_RENT
                        if left > 0:
                            try:
                                extra = await pool.buy_bulk(job.plan_id, left, price)
                            except Exception as e:
                                log.warning("pool buy_bulk job=%s: %s", job_id, e)
                    if extra > 0:
                        balance -= price * extra  # пул потратил деньги — учитываем в локальном балансе
                        last_meta = -1e9  # пул включает все аккаунты, часть extra куплена не с self.lk — форс refetch баланса
                        try:
                            # shield: если задание выключают (task.cancel) в этот момент,
                            # CancelledError будет доставлен сюда — без shield record_run
                            # не запустится и bought_count занизится (ср. комментарий выше
                            # про record_run(cnt), который вынесен ДО await-операций пула).
                            await asyncio.shield(self.db.record_run(job.id, extra, "ok"))
                        except Exception as db_err:
                            log.warning("hunt record_run job=%s extra=%d: %s", job_id, extra, db_err)
                    total = cnt + extra
                    log.info("hunt job=%s bought=%d (свой=%d, пул=%d, avail=%d, maxq=%d, blind=%s)",
                             job_id, total, cnt, extra, avail, maxq, avail <= 0)
                    tail = f" (+{extra} пулом)" if extra else ""
                    await self.notify(
                        f"✅ Куплено {total}{tail} ({job.service_name}). Остаток: {balance:.2f}")
                    last_blind = -1e9  # был сток — гонимся пачками без слепой паузы
                    continue
                else:
                    if avail > 0:
                        log.info("hunt job=%s probe avail=%d maxq=%d, просили %d, но rent=0/%s | сервер: %s",
                                 job_id, avail, maxq, n, st, getattr(self.lk, "last_rent_detail", ""))
                    await self._beat(job_id, now2, f"отказ: {st}")
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            return
        except Exception as e:
            # любой непойманный сбой не должен убивать охотника навсегда —
            # логируем и перезапускаем задание, если оно ещё включено.
            # Сначала убираем из dict — watchdog может перезапустить за время сна,
            # и тогда повторный create_task создал бы дублирующий цикл.
            log.exception("hunt_loop job=%s crashed, перезапуск: %s", job_id, e)
            self._tasks.pop(job_id, None)
            await asyncio.sleep(3)
            if job_id not in self._tasks:  # watchdog уже перезапустил — не дублируем
                job = await self.db.get_job(job_id)
                if job and job.enabled and job_id not in self._tasks:  # повторная проверка после await
                    self._tasks[job_id] = asyncio.create_task(self._hunt_loop(job_id))

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
