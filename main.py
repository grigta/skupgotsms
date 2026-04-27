from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.callback_answer import CallbackAnswerMiddleware

from autobuy import AutobuyManager
from bot.handlers import build_router
from config import settings
from db import DB
from gotsms_api import GotSmsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


async def prewarm(api: GotSmsClient) -> None:
    """Refresh services cache + already-warm plan caches in the background."""
    try:
        api.invalidate_cache("services_full:")
        api.invalidate_cache("services:")
        log.info("prewarm: fetching all services across pages…")
        services = await api.services_full(per_page=100)
        log.info("prewarm: %d services cached", len(services))
    except Exception as e:
        log.warning("prewarm services failed: %s", e)
        return

    # refresh any plan caches the user already touched
    plan_service_ids = {
        key.split(":")[1]
        for key in list(api._cache.keys())  # noqa: SLF001
        if key.startswith("plans:") and key.endswith(":1:100")
    }
    for svc_id in plan_service_ids:
        if not svc_id:
            continue
        try:
            api.invalidate_cache(f"plans:{svc_id}:")
            await api.plans_all(service_id=svc_id, per_page=100)
        except Exception as e:
            log.warning("prewarm plans for %s failed: %s", svc_id, e)


async def main() -> None:
    db = DB(settings.db_path)
    await db.init()

    api = GotSmsClient(settings.gotsms_api_token, base_url=settings.gotsms_base_url)

    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    async def notify(text: str) -> None:
        for uid in settings.telegram_user_ids:
            try:
                await bot.send_message(uid, text)
            except Exception as e:
                log.warning("notify %s failed: %s", uid, e)

    autobuy = AutobuyManager(db=db, api=api, notify=notify)
    autobuy.start()
    await autobuy.restore()

    # background pre-warm: refresh services cache periodically so the user
    # never has to wait for gotsms's slow /api/services on first click
    autobuy.scheduler.add_job(
        prewarm, "interval", minutes=5, args=[api], id="prewarm",
        next_run_time=None, max_instances=1, coalesce=True,
    )
    asyncio.create_task(prewarm(api))  # immediate first run

    dp = Dispatcher()
    # auto-answer all callback queries before the handler runs (kills the
    # client-side spinner instantly even if the handler is slow)
    dp.callback_query.middleware(CallbackAnswerMiddleware(pre=True))
    dp.include_router(build_router(api=api, db=db, autobuy=autobuy, allowed_user_ids=set(settings.telegram_user_ids)))

    log.info("starting bot for user_ids=%s", settings.telegram_user_ids)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await api.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
