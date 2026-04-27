from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from db import AutobuyJob
from gotsms_api import Plan, Service


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📨 SMS")],
            [KeyboardButton(text="🛒 Купить номер"), KeyboardButton(text="📱 Мои номера")],
            [KeyboardButton(text="🤖 Автобай")],
        ],
        resize_keyboard=True,
    )


def services_kb(services: list[Service], page: int, has_next: bool, prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for s in services:
        rows.append([InlineKeyboardButton(text=s.name, callback_data=f"{prefix}:svc:{s.id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:svcpage:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:svcpage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb(plans: list[Plan], page: int, has_next: bool, prefix: str, service_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in plans:
        country = f"{p.country_name} · " if p.country_name else ""
        label = f"{country}{p.duration} {p.duration_type} · {p.billing_type} · {p.price}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:plan:{p.id}")])
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:planpage:{service_id}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:planpage:{service_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К сервисам", callback_data=f"{prefix}:back")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def autobuy_list_kb(jobs: list[AutobuyJob]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for j in jobs:
        flag = "🟢" if j.enabled else "⚪️"
        rows.append([
            InlineKeyboardButton(
                text=f"{flag} {j.service_name} · {j.plan_label} · {j.interval_min}мин · куплено {j.bought_count}",
                callback_data=f"ab:open:{j.id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="➕ Новый автобай", callback_data="ab:new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def autobuy_job_kb(job: AutobuyJob) -> InlineKeyboardMarkup:
    toggle = "🛑 Выключить" if job.enabled else "▶️ Включить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle, callback_data=f"ab:toggle:{job.id}")],
        [InlineKeyboardButton(text="⏱ Интервал", callback_data=f"ab:interval:{job.id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ab:del:{job.id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="ab:back")],
    ])


def confirm_buy_kb(plan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Купить сейчас", callback_data=f"buy:confirm:{plan_id}")],
        [InlineKeyboardButton(text="🤖 В автобай", callback_data=f"ab:fromplan:{plan_id}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="buy:cancel")],
    ])
