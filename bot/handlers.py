from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from autobuy import AutobuyManager
from db import DB
from gotsms_api import GotSmsClient, GotSmsError, NoNumbersAvailable, InsufficientFunds, Plan

from .keyboards import (
    autobuy_job_kb,
    autobuy_list_kb,
    confirm_buy_kb,
    letters_kb,
    main_menu,
    plans_kb,
    services_kb,
)
from .states import BuyFlow, IntervalFlow

log = logging.getLogger("bot")
SERVICES_PER_PAGE = 12
PLANS_PER_PAGE = 12


def build_router(api: GotSmsClient, db: DB, autobuy: AutobuyManager, allowed_user_ids: set[int]) -> Router:
    r = Router()

    @r.message(F.from_user.id.func(lambda uid: uid not in allowed_user_ids))
    async def _block_others(m: Message):
        log.warning("rejected user %s", m.from_user.id if m.from_user else "?")

    @r.callback_query(F.from_user.id.func(lambda uid: uid not in allowed_user_ids))
    async def _block_others_cb(c: CallbackQuery):
        await c.answer("Доступ запрещён", show_alert=True)

    @r.message(Command("start"))
    @r.message(F.text == "/menu")
    async def cmd_start(m: Message, state: FSMContext):
        await state.clear()
        await m.answer(
            "Привет! Бот для gotsms.org.\n"
            "💰 Баланс — текущий счёт\n"
            "🛒 Купить номер — разовая покупка\n"
            "🤖 Автобай — авто-выкуп пока хватает баланса\n"
            "📱 Мои номера — активные аренды\n"
            "📨 SMS — непрочитанные сообщения",
            reply_markup=main_menu(),
        )

    # ───────── Balance ─────────
    @r.message(F.text == "💰 Баланс")
    async def show_balance(m: Message):
        try:
            bal = await api.balance()
            await m.answer(f"💰 Баланс: <b>{bal:.2f}</b>")
        except GotSmsError as e:
            await m.answer(f"Ошибка API {e.status}: <code>{e.payload}</code>")

    # ───────── My numbers ─────────
    @r.message(F.text == "📱 Мои номера")
    async def show_rents(m: Message):
        try:
            rents = await api.list_rents(status="active")
        except GotSmsError as e:
            await m.answer(f"Ошибка API {e.status}")
            return
        if not rents:
            await m.answer("Активных номеров нет.")
            return
        lines = ["<b>Активные номера:</b>"]
        for x in rents[:30]:
            lines.append(f"• <code>{x.phone}</code> — {x.service_name} (до {x.active_till or '—'})")
        await m.answer("\n".join(lines))

    # ───────── SMS ─────────
    @r.message(F.text == "📨 SMS")
    async def show_unread(m: Message):
        try:
            msgs = await api.unread_messages(mark_as_read=True, per_page=30)
        except GotSmsError as e:
            await m.answer(f"Ошибка API {e.status}")
            return
        if not msgs:
            await m.answer("Непрочитанных SMS нет.")
            return
        for sms in msgs[:20]:
            await m.answer(
                f"📨 <b>{sms.service_name}</b> · <code>{sms.phone}</code>\n"
                f"От: {sms.sender}\n"
                f"Код: <b>{sms.code or '—'}</b>\n"
                f"<i>{sms.body}</i>"
            )

    # ───────── Buy flow (one-shot) ─────────
    @r.message(F.text == "🛒 Купить номер")
    async def buy_start(m: Message, state: FSMContext):
        await state.clear()
        await state.update_data(prefix="buy")
        await _show_letters(m, prefix="buy", edit=False)

    # ───────── Autobuy menu ─────────
    @r.message(F.text == "🤖 Автобай")
    async def ab_menu(m: Message):
        jobs = await db.list_jobs()
        if not jobs:
            await m.answer(
                "У тебя нет автобаев.\nНажми «➕ Новый автобай» чтобы добавить.",
                reply_markup=autobuy_list_kb([]),
            )
            return
        await m.answer("🤖 Автобаи:", reply_markup=autobuy_list_kb(jobs))

    @r.callback_query(F.data == "ab:new")
    async def ab_new(c: CallbackQuery, state: FSMContext):
        await c.answer()
        await state.clear()
        await state.update_data(prefix="ab")
        await _show_letters(c.message, prefix="ab", edit=True)

    @r.callback_query(F.data.startswith("ab:open:"))
    async def ab_open(c: CallbackQuery):
        await c.answer()
        job_id = int(c.data.split(":")[2])
        job = await db.get_job(job_id)
        if not job:
            await c.answer("Не найдено", show_alert=True)
            return
        await _safe_edit(c.message, _job_text(job), reply_markup=autobuy_job_kb(job))

    @r.callback_query(F.data == "ab:back")
    async def ab_back(c: CallbackQuery):
        await c.answer()
        jobs = await db.list_jobs()
        await _safe_edit(c.message, "🤖 Автобаи:", reply_markup=autobuy_list_kb(jobs))

    @r.callback_query(F.data.startswith("ab:toggle:"))
    async def ab_toggle(c: CallbackQuery):
        await c.answer()
        job_id = int(c.data.split(":")[2])
        job = await db.get_job(job_id)
        if not job:
            return
        if job.enabled:
            await autobuy.disable(job_id)
        else:
            await autobuy.enable(job_id)
        job = await db.get_job(job_id)
        await _safe_edit(c.message, _job_text(job), reply_markup=autobuy_job_kb(job))

    @r.callback_query(F.data.startswith("ab:del:"))
    async def ab_del(c: CallbackQuery):
        await c.answer()
        job_id = int(c.data.split(":")[2])
        await autobuy.remove(job_id)
        jobs = await db.list_jobs()
        await _safe_edit(c.message, "Удалено.\n\n🤖 Автобаи:", reply_markup=autobuy_list_kb(jobs))

    @r.callback_query(F.data.startswith("ab:interval:"))
    async def ab_interval(c: CallbackQuery, state: FSMContext):
        await c.answer()
        job_id = int(c.data.split(":")[2])
        await state.set_state(IntervalFlow.waiting_value)
        await state.update_data(job_id=job_id)
        await c.message.answer(
            "Введи интервал в минутах (1–1440). Например: <code>5</code>"
        )

    @r.message(IntervalFlow.waiting_value)
    async def ab_interval_set(m: Message, state: FSMContext):
        try:
            value = int((m.text or "").strip())
        except ValueError:
            await m.answer("Нужно число от 1 до 1440.")
            return
        if not 1 <= value <= 1440:
            await m.answer("Нужно число от 1 до 1440.")
            return
        data = await state.get_data()
        job_id = data.get("job_id")
        await state.clear()
        await autobuy.set_interval(int(job_id), value)
        job = await db.get_job(int(job_id))
        await m.answer(_job_text(job), reply_markup=autobuy_job_kb(job))

    # ───────── Service / plan flow shared ─────────
    @r.callback_query(F.data.regexp(r"^(buy|ab):letters$"))
    async def cb_letters(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix = c.data.split(":")[0]
        await _show_letters(c.message, prefix=prefix, edit=True)

    @r.callback_query(F.data.regexp(r"^(buy|ab):letter:.+$"))
    async def cb_letter_pick(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix, _, letter = c.data.split(":", 2)
        await state.update_data(prefix=prefix, letter=letter)
        await _show_services_by_letter(c.message, letter=letter, page=1, prefix=prefix, edit=True)

    @r.callback_query(F.data.regexp(r"^(buy|ab):svcpage:[^:]+:\d+$"))
    async def cb_svc_page(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix, _, letter, page_s = c.data.split(":")
        await _show_services_by_letter(c.message, letter=letter, page=int(page_s), prefix=prefix, edit=True)

    @r.callback_query(F.data.regexp(r"^(buy|ab):svc:.+$"))
    async def cb_svc_pick(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix, _, svc_id = c.data.split(":", 2)
        await state.update_data(prefix=prefix, service_id=svc_id)
        await _show_plans(c.message, state, service_id=svc_id, page=1, prefix=prefix, edit=True)

    @r.callback_query(F.data.regexp(r"^(buy|ab):planpage:[^:]+:\d+$"))
    async def cb_plan_page(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix, _, svc_id, page_s = c.data.split(":")
        await _show_plans(c.message, state, service_id=svc_id, page=int(page_s), prefix=prefix, edit=True)

    @r.callback_query(F.data.regexp(r"^(buy|ab):back$"))
    async def cb_back_to_svc(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix = c.data.split(":")[0]
        data = await state.get_data()
        letter = data.get("letter")
        if letter:
            await _show_services_by_letter(c.message, letter=letter, page=1, prefix=prefix, edit=True)
        else:
            await _show_letters(c.message, prefix=prefix, edit=True)

    @r.callback_query(F.data.regexp(r"^(buy|ab):cancel$"))
    async def cb_cancel(c: CallbackQuery, state: FSMContext):
        await c.answer()
        await state.clear()
        await _safe_edit(c.message, "Отменено.")

    @r.callback_query(F.data.regexp(r"^(buy|ab):plan:.+$"))
    async def cb_plan_pick(c: CallbackQuery, state: FSMContext):
        await c.answer()
        prefix, _, plan_id = c.data.split(":", 2)
        data = await state.get_data()
        svc_id = data.get("service_id")
        plan = await _find_plan(svc_id, plan_id) if svc_id else None
        if not plan:
            await _safe_edit(c.message, "План не найден, попробуй ещё раз.")
            return

        if prefix == "buy":
            await _safe_edit(c.message, _plan_text(plan), reply_markup=confirm_buy_kb(plan.id))
        else:  # ab
            from config import settings
            job_id = await db.add_job(
                plan_id=plan.id,
                service_name=plan.service_name,
                plan_label=_plan_label(plan),
                interval_min=settings.default_autobuy_interval_min,
            )
            await autobuy.enable(job_id)
            job = await db.get_job(job_id)
            await _safe_edit(c.message, 
                f"🤖 Автобай создан и запущен.\n\n{_job_text(job)}",
                reply_markup=autobuy_job_kb(job),
            )
        await state.clear()

    @r.callback_query(F.data.startswith("buy:confirm:"))
    async def cb_buy_confirm(c: CallbackQuery, state: FSMContext):
        await c.answer("Покупаю…")
        plan_id = c.data.split(":", 2)[2]
        await state.clear()
        try:
            rent = await api.create_rent(plan_id)
        except NoNumbersAvailable:
            await _safe_edit(c.message, "😕 Свободных номеров сейчас нет, попробуй позже.")
            return
        except InsufficientFunds:
            await _safe_edit(c.message, "💸 Недостаточно средств.")
            return
        except GotSmsError as e:
            await _safe_edit(c.message, f"Ошибка {e.status}: <code>{e.payload}</code>")
            return
        await _safe_edit(c.message, 
            f"✅ Куплен <code>{rent.phone}</code>\n"
            f"Сервис: <b>{rent.service_name}</b>\n"
            f"Цена: {rent.price}\n"
            f"Активен до: {rent.active_till or '—'}"
        )

    @r.callback_query(F.data == "buy:cancel")
    async def cb_buy_cancel(c: CallbackQuery, state: FSMContext):
        await c.answer()
        await state.clear()
        await _safe_edit(c.message, "Отменено.")

    @r.callback_query(F.data.startswith("ab:fromplan:"))
    async def cb_ab_from_plan(c: CallbackQuery, state: FSMContext):
        await c.answer()
        plan_id = c.data.split(":", 2)[2]
        data = await state.get_data()
        svc_id = data.get("service_id")
        plan = await _find_plan(svc_id, plan_id) if svc_id else None
        if not plan:
            await _safe_edit(c.message, "План не найден, попробуй ещё раз.")
            return
        from config import settings
        job_id = await db.add_job(
            plan_id=plan.id,
            service_name=plan.service_name,
            plan_label=_plan_label(plan),
            interval_min=settings.default_autobuy_interval_min,
        )
        await autobuy.enable(job_id)
        job = await db.get_job(job_id)
        await _safe_edit(c.message, 
            f"🤖 Автобай создан и запущен.\n\n{_job_text(job)}",
            reply_markup=autobuy_job_kb(job),
        )
        await state.clear()

    # ───────── helpers ─────────
    async def _safe_edit(msg: Message, text: str, reply_markup=None) -> None:
        """Edit message; swallow 'not modified', log unexpected errors. Never sends a duplicate."""
        try:
            await msg.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            es = str(e).lower()
            if "message is not modified" in es or "message can't be edited" in es:
                return
            log.warning("edit_text failed: %s", e)

    def _bucket(name: str) -> str:
        if not name:
            return "#"
        ch = name[0].upper()
        if ch.isalpha() or ch.isdigit():
            return ch
        return "#"

    async def _fetch_full_services(target: Message, edit: bool) -> tuple[list, Message, bool] | None:
        """Returns (services, target, edit) — handles loading placeholder and errors."""
        cold = api._cache_get("services_full:200") is None  # noqa: SLF001
        if cold and edit:
            await _safe_edit(target, "⏳ Загружаю сервисы…")
        elif cold:
            target = await target.answer("⏳ Загружаю все сервисы… (gotsms долго отвечает, ~20 сек)")
            edit = True
        try:
            services = await api.services_full(per_page=200)
        except GotSmsError as e:
            await target.answer(f"Ошибка API {e.status}")
            return None
        return services, target, edit

    async def _show_letters(target: Message, prefix: str, edit: bool = False) -> None:
        result = await _fetch_full_services(target, edit)
        if result is None:
            return
        all_services, target, edit = result
        counts: dict[str, int] = {}
        for s in all_services:
            counts[_bucket(s.name)] = counts.get(_bucket(s.name), 0) + 1
        text = f"Сервисов: <b>{len(all_services)}</b>. Выбери букву:"
        kb = letters_kb(counts, prefix=prefix)
        if edit:
            await _safe_edit(target, text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)

    async def _show_services_by_letter(target: Message, letter: str, page: int, prefix: str, edit: bool = False) -> None:
        result = await _fetch_full_services(target, edit)
        if result is None:
            return
        all_services, target, edit = result
        filtered = [s for s in all_services if _bucket(s.name) == letter]
        total = len(filtered)
        start = (page - 1) * SERVICES_PER_PAGE
        end = start + SERVICES_PER_PAGE
        chunk = filtered[start:end]
        has_next = end < total
        text = f"<b>{letter}</b> · {total} сервис(ов). Выбери:"
        kb = services_kb(chunk, page=page, has_next=has_next, prefix=prefix, letter=letter)
        if edit:
            await _safe_edit(target, text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)

    async def _show_plans(target: Message, state: FSMContext, service_id: str, page: int, prefix: str, edit: bool = False) -> None:
        cold = api._cache_get(f"plans:{service_id}::::1:200") is None  # noqa: SLF001
        if cold and edit:
            await _safe_edit(target, "⏳ Загружаю планы…")
        elif cold:
            target = await target.answer("⏳ Загружаю планы… (gotsms бывает долго отвечает)")
            edit = True

        try:
            all_plans = await api.plans_all(service_id=service_id, per_page=200)
        except GotSmsError as e:
            await target.answer(f"Ошибка API {e.status}")
            return

        total = len(all_plans)
        start = (page - 1) * PLANS_PER_PAGE
        end = start + PLANS_PER_PAGE
        chunk = all_plans[start:end]
        has_next = end < total
        if total == 0:
            text = "Для этого сервиса нет доступных планов."
        else:
            text = f"Сервис: <b>{all_plans[0].service_name}</b> · {total} планов\nВыбери план:"
        kb = plans_kb(chunk, page=page, has_next=has_next, prefix=prefix, service_id=service_id)
        if edit:
            await _safe_edit(target, text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)

    async def _find_plan(service_id: str, plan_id: str) -> Plan | None:
        for p in await api.plans_all(service_id=service_id, per_page=200):
            if p.id == plan_id:
                return p
        return None

    return r


def _plan_label(p: Plan) -> str:
    country = f"{p.country_name} · " if p.country_name else ""
    return f"{country}{p.duration} {p.duration_type} · {p.billing_type} · {p.price}"


def _plan_text(p: Plan) -> str:
    return (
        f"<b>{p.service_name}</b>\n"
        f"Страна: {p.country_name or '—'}\n"
        f"Длительность: {p.duration} {p.duration_type}\n"
        f"Биллинг: {p.billing_type}\n"
        f"Цена: <b>{p.price}</b>\n\n"
        "Купить или поставить в автобай?"
    )


def _job_text(job) -> str:
    flag = "🟢 включен" if job.enabled else "⚪️ выключен"
    return (
        f"<b>{job.service_name}</b>\n"
        f"План: {job.plan_label}\n"
        f"Интервал: {job.interval_min} мин\n"
        f"Куплено всего: {job.bought_count}\n"
        f"Последний запуск: {job.last_run_at or '—'} ({job.last_status or '—'})\n"
        f"Статус: {flag}"
    )
