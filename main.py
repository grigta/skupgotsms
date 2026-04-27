from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

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


async def main() -> None:
    db = DB(settings.db_path)
    await db.init()

    api = GotSmsClient(settings.gotsms_api_token, base_url=settings.gotsms_base_url)

    bot = Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    async def notify(text: str) -> None:
        try:
            await bot.send_message(settings.telegram_user_id, text)
        except Exception as e:
            log.warning("notify failed: %s", e)

    autobuy = AutobuyManager(db=db, api=api, notify=notify)
    autobuy.start()
    await autobuy.restore()

    dp = Dispatcher()
    dp.include_router(build_router(api=api, db=db, autobuy=autobuy, allowed_user_id=settings.telegram_user_id))

    log.info("starting bot for user_id=%s", settings.telegram_user_id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await api.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
