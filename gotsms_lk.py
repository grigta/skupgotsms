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
    def __init__(self, session_cookie: str, xsrf_cookie: str, user_agent: str, base_url: str = "https://app.gotsms.org"):
        self.base = base_url
        self._cookies = {"gotsms_session": session_cookie, "XSRF-TOKEN": xsrf_cookie}
        self._ua = user_agent
        self._cli = httpx.AsyncClient(
            base_url=base_url, cookies=self._cookies,
            headers={"User-Agent": user_agent}, timeout=40.0, follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        self._csrf: str | None = None
        self._uri: str | None = None
        self._modal_snapshot: str | None = None  # raw JSON-строка snapshot `livewire-ui-modal`

    async def aclose(self) -> None:
        await self._cli.aclose()

    async def update_cookies(self, session_cookie: str, xsrf_cookie: str) -> None:
        """Заменить cookie-сессию на лету (после ручного обновления через бота)."""
        await self._cli.aclose()
        self._cookies = {"gotsms_session": session_cookie, "XSRF-TOKEN": xsrf_cookie}
        self._cli = httpx.AsyncClient(
            base_url=self.base, cookies=self._cookies,
            headers={"User-Agent": self._ua}, timeout=40.0, follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
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
        r = await self._cli.get("/rents/create")
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
        r = await self._cli.post(self._uri, json=body, headers={
            "X-Livewire": "true", "Content-Type": "application/json",
            "X-CSRF-TOKEN": self._csrf, "Accept": "*/*", "Referer": self.base + "/rents/create",
        })
        if r.status_code == 419:  # CSRF/сессия истекла
            raise LkAuthError("419 — сессия истекла")
        if r.status_code == 401 or r.status_code == 403:
            raise LkAuthError(f"{r.status_code} — не авторизован")
        if r.status_code != 200:
            raise LkError(f"livewire {r.status_code}: {r.text[:200]}")
        return r.json()

    # ───────── bulk buy ─────────
    async def buy(self, plan_id: str, quantity: int) -> tuple[int, str]:
        """Купить `quantity` (≤25) номеров плана одним запросом.
        Возвращает (куплено, статус: ok|no_numbers|insufficient_funds|err)."""
        if not self._modal_snapshot:
            await self.bootstrap()
        qty = max(1, min(quantity, MAX_PER_RENT))

        # 1) открыть модалку → получить snapshot area-code-rent-modal
        open_resp = await self._post([{
            "snapshot": self._modal_snapshot, "updates": {},
            "calls": [{"path": "", "method": "__dispatch",
                       "params": ["openModal", {"component": MODAL, "arguments": {"planId": plan_id}}]}],
        }])
        host = open_resp["components"][0]
        # НЕ мутируем self._modal_snapshot: начальный (закрытая модалка) snapshot
        # валиден для каждого openModal. Снапшот из ответа = "открытое" состояние,
        # переиспользовать его для след. openModal нельзя (модалка уже открыта).
        eff_html = host.get("effects", {}).get("html", "")
        modal = next(((raw, s) for (n, raw, s) in self._snapshots(eff_html) if n == MODAL), None)
        if not modal:
            raise LkError("модалка не отрисовалась (нет area-code snapshot)")
        modal_raw, modal_s = modal
        max_q = int(modal_s["data"].get("maxQuantity") or MAX_PER_RENT)
        qty = min(qty, max_q)

        # 2) set quantity + rent
        buy_resp = await self._post([{
            "snapshot": modal_raw,
            "updates": {"selectedAreaCode": "", "quantity": qty},
            "calls": [{"path": "", "method": "rent", "params": []}],
        }])
        return self._parse_rent(buy_resp)

    @staticmethod
    def _parse_rent(resp: dict) -> tuple[int, str]:
        txt = json.dumps(resp).lower()
        # успех: dispatch notify "Successfully rented N number(s)"
        m = re.search(r"successfully rented\s+(\d+)\s+number", txt)
        if m:
            return int(m.group(1)), "ok"
        if "no number" in txt or "not available" in txt or "out of stock" in txt or "sold out" in txt:
            return 0, "no_numbers"
        if "insufficient" in txt or "not enough" in txt or "balance" in txt:
            return 0, "insufficient_funds"
        # иногда успех без числа — но notify есть
        if "rented" in txt and "success" in txt:
            return 0, "ok"
        return 0, "err"


class LkPool:
    """Пул ЛК-аккаунтов. Сервер gotsms сериализует покупки ОДНОГО аккаунта
    (lock на балансе) и ограничивает 25/запрос — потолок ~74 номера/мин на
    аккаунт. Несколько аккаунтов работают параллельно (каждый свой lock),
    скорости складываются: N аккаунтов ≈ N×74/мин."""

    def __init__(self, accounts: list[dict], user_agent: str, base_url: str = "https://app.gotsms.org"):
        # accounts: [{"session": "...", "xsrf": "..."}, ...]
        self.clients = [LkClient(a["session"], a["xsrf"], user_agent, base_url) for a in accounts]

    async def aclose(self) -> None:
        for c in self.clients:
            await c.aclose()

    @property
    def size(self) -> int:
        return len(self.clients)

    async def _drain_one(self, cli: LkClient, plan_id: str, target: int, price: float, balances: dict, idx: int) -> int:
        """Один аккаунт последовательно выкупает по 25, пока не возьмёт `target`
        или пул/баланс не иссякнут. Баланс аккаунта ведём в balances[idx]."""
        bought = 0
        while bought < target:
            n = min(25, target - bought)
            if price > 0 and balances.get(idx, 1e9) < price:
                break
            try:
                cnt, st = await cli.buy(plan_id, n)
            except (LkAuthError, LkError) as e:
                log.warning("pool acct#%d: %s", idx, e)
                break
            if cnt <= 0:
                break  # no_numbers / insufficient
            bought += cnt
            if price > 0 and idx in balances:
                balances[idx] -= price * cnt
        return bought

    async def buy_bulk(self, plan_id: str, total: int, price: float = 0.0, balances: dict | None = None) -> int:
        """Выкупить до `total` номеров плана, раскидав работу по аккаунтам
        ПАРАЛЛЕЛЬНО. Возвращает фактически купленное."""
        if not self.clients:
            return 0
        balances = balances or {}
        share = max(1, -(-total // len(self.clients)))  # ceil
        tasks = [self._drain_one(c, plan_id, share, price, balances, i) for i, c in enumerate(self.clients)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return sum(r for r in results if isinstance(r, int))
