"""ЛК-клиент gotsms через Livewire — bulk-покупка до 25 номеров одним запросом.

Публичный API (gotsms_api.py) лимитирован 30 req/min. Веб-ЛК на Laravel Livewire
покупает пачкой (quantity ≤ maxQuantity=25) и НЕ лимитируется 30/мин — это снимает
потолок скорости. Работает на cookie-сессии ЛК (см. LIVEWIRE_NOTES.md).

Flow одной bulk-покупки:
  1. bootstrap: GET /rents/create → csrf, livewire uri, snapshot `livewire-ui-modal`
  2. openModal(planId) → snapshot компонента `area-code-rent-modal`
  3. set quantity + call rent → "Successfully rented N number(s)"
"""
from __future__ import annotations

import asyncio
import html as htmlmod
import json
import logging
import re

import httpx

log = logging.getLogger("gotsms_lk")

MODAL = "app.rent.modals.area-code-rent-modal"
MAX_PER_RENT = 25


class LkError(Exception):
    pass


class LkAuthError(LkError):
    """Сессия протухла / не залогинен — нужен свежий cookie (или авто-логин)."""


class LkClient:
    def __init__(self, session_cookie: str, xsrf_cookie: str, user_agent: str,
                 base_url: str = "https://app.gotsms.org", proxy: str | None = None):
        self.base = base_url
        self._cookies = {"gotsms_session": session_cookie, "XSRF-TOKEN": xsrf_cookie}
        self._ua = user_agent
        self._proxy = proxy or None  # "socks5://user:pass@host:port" | "http://..."
        self._cli = self._new_client()
        self._csrf: str | None = None
        self._uri: str | None = None
        self._modal_snapshot: str | None = None  # raw JSON-строка snapshot `livewire-ui-modal`
        self.last_rent_detail: str = ""  # что ответил сервер на последний неудачный rent

    def _new_client(self) -> httpx.AsyncClient:
        """httpx-клиент этого аккаунта. Прокси задаётся на аккаунт: при работе
        пулом каждый аккаунт ходит со своего адреса, а не все с одного IP."""
        kw = {}
        if self._proxy:
            kw["proxy"] = self._proxy
        return httpx.AsyncClient(
            base_url=self.base, cookies=self._cookies,
            headers={"User-Agent": self._ua}, timeout=40.0, follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            **kw,
        )

    async def aclose(self) -> None:
        await self._cli.aclose()

    async def update_cookies(self, session_cookie: str, xsrf_cookie: str) -> None:
        """Заменить cookie-сессию на лету (после ручного обновления через бота)."""
        await self._cli.aclose()
        self._cookies = {"gotsms_session": session_cookie, "XSRF-TOKEN": xsrf_cookie}
        self._cli = self._new_client()
        self._csrf = None
        self._uri = None
        self._modal_snapshot = None  # форс rebootstrap на следующей покупке

    async def check_alive(self) -> bool:
        """Жива ли сессия (проверка после обновления cookie)."""
        try:
            await self.bootstrap()
            return True
        except LkAuthError:
            return False
        except LkError:
            return True  # сессия жива, просто другая ошибка

    async def balance(self) -> float | None:
        """Баланс аккаунта (через Livewire-компонент landing.balance).
        None — сессия мертва / не удалось получить."""
        try:
            r = await self._cli.get("/rents/create")
        except Exception:
            return None
        if r.status_code != 200:
            return None
        doc = r.text
        m = re.search(r"window\.livewireScriptConfig\s*=\s*(\{.*?\})\s*;", doc)
        if not m:
            return None
        cfg = json.loads(m.group(1))
        csrf = cfg["csrf"]
        uri = cfg["uri"].replace(self.base, "")
        bsnap = next((raw for (n, raw, s) in self._snapshots(doc) if n == "landing.balance"), None)
        if not bsnap:
            return None
        try:
            resp = await self._cli.post(uri, json={"_token": csrf, "components": [{
                "snapshot": bsnap, "updates": {}, "calls": [{"path": "", "method": "$refresh", "params": []}]}]},
                headers={"X-Livewire": "true", "Content-Type": "application/json", "X-CSRF-TOKEN": csrf})
            nums = re.findall(r"([0-9]+\.[0-9]+)", resp.text)
            return float(nums[0]) if nums else None
        except Exception:
            return None

    # ───────── helpers ─────────
    @staticmethod
    def _snapshots(doc: str) -> list[tuple[str, str, dict]]:
        out = []
        for m in re.finditer(r'wire:snapshot="([^"]+)"', doc):
            raw = htmlmod.unescape(m.group(1))
            try:
                s = json.loads(raw)
                out.append((s.get("memo", {}).get("name", ""), raw, s))
            except Exception:
                pass
        return out

    async def bootstrap(self) -> None:
        """Свежие csrf, livewire-uri и snapshot модального хоста со страницы покупки."""
        try:
            r = await self._cli.get("/rents/create")
        except httpx.HTTPError as e:
            raise LkError(f"network: {type(e).__name__}") from e
        if r.status_code in (301, 302) or "/login" in str(r.headers.get("location", "")):
            raise LkAuthError("сессия ЛК протухла (редирект на /login)")
        if r.status_code != 200:
            raise LkError(f"GET /rents/create -> {r.status_code}")
        doc = r.text
        m = re.search(r'window\.livewireScriptConfig\s*=\s*(\{.*?\})\s*;', doc)
        if not m:
            raise LkError("livewireScriptConfig не найден")
        cfg = json.loads(m.group(1))
        self._csrf = cfg["csrf"]
        self._uri = cfg["uri"].replace(self.base, "")  # путь /livewire-XXXX/update
        host = next(((raw, s) for (n, raw, s) in self._snapshots(doc) if n == "livewire-ui-modal"), None)
        if not host:
            raise LkError("snapshot livewire-ui-modal не найден")
        self._modal_snapshot = host[0]
        log.info("LK bootstrap ok: uri=%s", self._uri)

    async def _post(self, components: list[dict]) -> dict:
        if not self._csrf or not self._uri:
            await self.bootstrap()
        body = {"_token": self._csrf, "components": components}
        try:
            r = await self._cli.post(self._uri, json=body, headers={
                "X-Livewire": "true", "Content-Type": "application/json",
                "X-CSRF-TOKEN": self._csrf, "Accept": "*/*", "Referer": self.base + "/rents/create",
            })
        except httpx.HTTPError as e:  # timeout/connect/522-read и т.п. — не роняем цикл
            raise LkError(f"network: {type(e).__name__}") from e
        if r.status_code == 419:  # CSRF протух — нужен ребутстрап, сессия может быть жива
            self._csrf = None          # форс bootstrap при следующем _post()
            self._modal_snapshot = None
            raise LkError("419 CSRF expired")
        if r.status_code == 401 or r.status_code == 403:
            raise LkAuthError(f"{r.status_code} — не авторизован")
        if r.status_code != 200:
            raise LkError(f"livewire {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except Exception as e:
            raise LkError(f"bad JSON in Livewire response: {r.text[:200]}") from e

    # ───────── bulk buy ─────────
    @staticmethod
    def _available_count(data: dict) -> int:
        """Сумма доступных номеров по area-кодам (0 = пул пуст)."""
        total = 0

        def walk(x):
            nonlocal total
            if isinstance(x, dict):
                c = x.get("count")
                if isinstance(c, int):
                    total += c
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(data.get("availableAreaCodes"))
        return total

    @staticmethod
    def _unwrap(val):
        """Livewire упаковывает коллекции как [значение, {"s": "arr"}] — достаём значение."""
        if isinstance(val, list) and len(val) == 2 and isinstance(val[1], dict) and "s" in val[1]:
            return val[0]
        return val

    def _pick_area_code(self, data: dict) -> str:
        """Код региона с наибольшим наличием. "" — если план не требует кода
        или кодов нет (сервер сам выберет номер)."""
        codes = self._unwrap(data.get("availableAreaCodes"))
        if not isinstance(codes, list) or not codes:
            return ""
        best, best_cnt = "", -1
        for item in codes:
            if not isinstance(item, dict):
                continue
            code = item.get("area_code") or item.get("areaCode") or item.get("code") or item.get("value")
            cnt = item.get("count")
            cnt = cnt if isinstance(cnt, int) else 0
            if code and cnt > best_cnt:
                best, best_cnt = str(code), cnt
        return best

    async def probe(self, plan_id: str) -> tuple[str | None, int, int, str]:
        """Дёшево (~0.8с, openModal) узнать наличие номеров плана.
        Возвращает (snapshot модалки, доступно, maxQuantity, area-код).
        Пустой пул → (snapshot, 0, maxq, код).

        Ничего не пишет в self — поэтому probe можно гонять в несколько
        параллельных дорожек (конвейер) без гонок за общее состояние.
        Snapshot можно сразу передать в `rent` для немедленного выкупа.

        ВАЖНО: snapshot хоста НЕ перезаписываем ответом. В ответе хост уже
        с открытой модалкой в стеке, и следующий openModal поверх неё Livewire
        считает «без изменений» — html пустой, модалка не находится. Держим
        снапшот из bootstrap неизменным: каждый probe стартует с чистого хоста."""
        if not self._modal_snapshot:
            await self.bootstrap()
        try:
            open_resp = await self._post([{
                "snapshot": self._modal_snapshot, "updates": {},
                "calls": [{"path": "", "method": "__dispatch",
                           "params": ["openModal", {"component": MODAL, "arguments": {"planId": plan_id}}]}],
            }])
        except LkError:
            self._modal_snapshot = None  # snapshot протух (перезапуск gotsms) — форс-ребутстрап
            raise
        components = open_resp.get("components") or []
        if not components:
            self._modal_snapshot = None
            raise LkError("probe: пустой components в ответе")
        eff_html = (components[0].get("effects") or {}).get("html") or ""
        modal = next(((raw, s) for (n, raw, s) in self._snapshots(eff_html) if n == MODAL), None)
        if not modal:
            self._modal_snapshot = None  # пустой ответ — snapshot устарел, форс-ребутстрап
            return None, 0, 0, ""
        modal_raw, modal_s = modal
        data = modal_s["data"]
        counts = self._available_count(data)
        maxq = int(data.get("maxQuantity") or 0)
        # area-code count бывает занижен/нулевой (напр. Bank of America), хотя
        # номер реально доступен — подстраховываемся maxQuantity (>1 = есть сток).
        avail = counts if counts > 0 else (maxq if maxq > 1 else 0)
        return modal_raw, avail, maxq, self._pick_area_code(data)

    async def rent(self, modal_raw: str, qty: int, area_code: str | None = None) -> tuple[int, str]:
        """Выкуп по уже полученному snapshot модалки (из probe).

        `area_code` — код региона из probe. Пустой = «любой»; сервер принимает
        пустую строку только если план не требует выбора кода."""
        qty = max(1, min(qty, MAX_PER_RENT))
        code = area_code or ""
        buy_resp = await self._post([{
            "snapshot": modal_raw,
            "updates": {"selectedAreaCode": code or "", "quantity": qty},
            "calls": [{"path": "", "method": "rent", "params": []}],
        }])
        cnt, status = self._parse_rent(buy_resp, qty)
        if cnt <= 0:
            # Сохраняем, что именно ответил сервер — иначе отказ неотличим от отказа.
            self.last_rent_detail = self._explain(buy_resp)
            log.info("rent qty=%d code=%r -> %s | сервер: %s", qty, code, status, self.last_rent_detail)
        return cnt, status

    @staticmethod
    def _explain(resp: dict) -> str:
        """Человеческая выжимка из Livewire-ответа: тексты уведомлений/ошибок."""
        txt = json.dumps(resp, ensure_ascii=False)
        bits: list[str] = []
        for pat in (r'"notify"[^]]{0,200}', r'"message"\s*:\s*"([^"]{0,200})"',
                    r'"error[s]?"\s*:\s*("[^"]{0,200}"|\{[^}]{0,200}\})'):
            for m in re.findall(pat, txt):
                s = m if isinstance(m, str) else str(m)
                if s and s not in bits:
                    bits.append(s[:200])
        return " | ".join(bits[:4]) if bits else txt[:300]

    async def buy(self, plan_id: str, quantity: int) -> tuple[int, str]:
        """Купить до `quantity` номеров. Сначала probe (0.8с): если пул
        пуст — мгновенно no_numbers (не висим 21с на пустом rent).
        Количество зажимается в maxQuantity — заказ сверх потолка сервер отвергает."""
        modal_raw, available, maxq, code = await self.probe(plan_id)
        if not modal_raw or available <= 0:
            return 0, "no_numbers"
        cap = maxq if maxq > 0 else MAX_PER_RENT
        return await self.rent(modal_raw, min(quantity, available, cap, MAX_PER_RENT), code)

    @staticmethod
    def _parse_rent(resp: dict, qty_requested: int = 0) -> tuple[int, str]:
        txt = json.dumps(resp).lower()
        # успех: dispatch notify "Successfully rented N number(s)"
        m = re.search(r"successfully rented\s+(\d+)\s+number", txt)
        if m:
            return int(m.group(1)), "ok"
        if "no number" in txt or "not available" in txt or "out of stock" in txt or "sold out" in txt:
            return 0, "no_numbers"
        # fallback-успех раньше "balance": livewire-ответы содержат поле balance в
        # состоянии компонента, и "balance" in txt давал ложный insufficient_funds
        # когда уведомление приходило без точной фразы "successfully rented N number(s)".
        if "rented" in txt and "success" in txt:
            return max(1, qty_requested), "ok"
        if "insufficient" in txt or "not enough" in txt or "balance" in txt:
            return 0, "insufficient_funds"
        return 0, "err"


class LkPool:
    """Пул ЛК-аккаунтов. Сервер gotsms сериализует покупки ОДНОГО аккаунта
    (lock на балансе) и ограничивает 25/запрос — потолок ~74 номера/мин на
    аккаунт. Несколько аккаунтов работают параллельно (каждый свой lock),
    скорости складываются: N аккаунтов ≈ N×74/мин."""

    def __init__(self, accounts: list[dict], user_agent: str, base_url: str = "https://app.gotsms.org"):
        # accounts: [{"session": "...", "xsrf": "...", "proxy": "socks5://..."}, ...]
        self.clients = [
            LkClient(a["session"], a["xsrf"], user_agent, base_url, proxy=a.get("proxy"))
            for a in accounts
        ]

    @classmethod
    def from_clients(cls, clients: list[LkClient]) -> "LkPool":
        """Пул поверх уже живых клиентов (переиспользуем keep-alive соединения
        вместо пересоздания httpx-клиента на каждый выкуп)."""
        p = cls.__new__(cls)
        p.clients = list(clients)
        return p

    async def aclose(self) -> None:
        for c in self.clients:
            await c.aclose()

    @property
    def size(self) -> int:
        return len(self.clients)

    async def _drain_one(self, cli: LkClient, plan_id: str, budget: dict, lock: asyncio.Lock,
                         price: float, balances: dict, idx: int) -> int:
        """Один аккаунт тянет из ОБЩЕГО бюджета, пока тот не иссякнет или пул
        не опустеет. Бюджет общий и под локом — иначе N аккаунтов выкупят
        N×target и вылезут за buy_limit (это реальные деньги).

        Если бюджет разобран, но другие аккаунты ещё в полёте — ЖДЁМ, а не
        выходим: они вернут недобор, и работа достанется нам. Иначе аккаунт
        с большим стоком мог не поучаствовать вовсе."""
        bought = 0
        while True:
            async with lock:
                if price > 0 and balances.get(idx, 1e9) < price:
                    break  # денег нет — этот аккаунт закончил
                if budget["n"] <= 0:
                    if budget["inflight"] <= 0:
                        break  # никто не работает и пополнить бюджет некому
                    take = 0  # ждём возврата недобора от других
                else:
                    take = min(MAX_PER_RENT, budget["n"])
                    budget["n"] -= take  # резервируем свою долю заранее
                    budget["inflight"] += 1
            if take == 0:
                await asyncio.sleep(0.05)
                continue
            try:
                cnt, st = await cli.buy(plan_id, take)
            except Exception as e:
                log.warning("pool acct#%d: %s", idx, e)
                async with lock:
                    budget["n"] += take  # не смогли — возвращаем долю в общий котёл
                    budget["inflight"] -= 1
                break
            async with lock:
                budget["n"] += take - cnt  # недобор возвращаем другим аккаунтам
                budget["inflight"] -= 1
            if cnt <= 0:
                break  # no_numbers / insufficient — этому аккаунту тут ловить нечего
            bought += cnt
            if price > 0 and idx in balances:
                balances[idx] -= price * cnt
        return bought

    async def buy_bulk(self, plan_id: str, total: int, price: float = 0.0, balances: dict | None = None) -> int:
        """Выкупить до `total` номеров плана, раскидав работу по аккаунтам
        ПАРАЛЛЕЛЬНО. Возвращает фактически купленное (никогда не больше `total`)."""
        if not self.clients or total <= 0:
            return 0
        balances = balances or {}
        budget = {"n": total, "inflight": 0}  # inflight — сколько покупок сейчас летит
        lock = asyncio.Lock()
        tasks = [self._drain_one(c, plan_id, budget, lock, price, balances, i)
                 for i, c in enumerate(self.clients)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                log.warning("pool worker упал: %s", r)
        return sum(r for r in results if isinstance(r, int))
