"""Запуск polling. Один процесс на токен."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher

from config import load_config
from handlers.audit_flow import build_router
from services.cases import CaseStore
from services.deepseek_client import DeepSeekClient


class AllowList(BaseMiddleware):
    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None or user.id not in self.allowed:
            return None  # чужим — молчание
        return await handler(event, data)


def load_dotenv(path: Path) -> None:
    """Для локального запуска: KEY=VALUE из .env, не перезаписывая окружение."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    prompt = cfg.load_prompt()
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = CaseStore(cfg.db_path)
    stuck = store.recover_stuck()
    if stuck:
        logging.info("recovered %d stuck cases -> failed", stuck)
    ds = DeepSeekClient(cfg, prompt)
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    dp.message.outer_middleware(AllowList(cfg.allowed_user_ids))
    dp.callback_query.outer_middleware(AllowList(cfg.allowed_user_ids))

    @dp.errors()
    async def on_error(event):
        logging.getLogger("bot").exception("handler failed", exc_info=event.exception)
        return True

    dp.include_router(build_router(cfg, store, ds))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
