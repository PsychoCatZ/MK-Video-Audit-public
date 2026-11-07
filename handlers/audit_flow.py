"""Диалог с пользователем: заметки → «Собрать» → три блока. Вызовов API здесь нет — только через DeepSeekClient."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from config import Config
from services import cases as C
from services.cases import CaseStore
from services.deepseek_client import DeepSeekClient, DeepSeekError
from services.formatter import has_all_blocks, md_to_tg_html, parse_blocks, split_header, split_long

log = logging.getLogger("flow")

HINT_EMPTY = "Заметок пока нет. Пришлите текстовые наблюдения по карточке (можно несколькими сообщениями), затем нажмите «Собрать сценарий»."
MSG_TEMP = "Сервис анализа временно недоступен. Материалы сохранены, анализ можно повторить позже."
MSG_FATAL = "Не удалось выполнить анализ (проблема с доступом к сервису модели). Материалы сохранены, анализ можно повторить позже. Сообщите администратору."
MSG_LENGTH = "Модель не уложилась в лимит ответа. Материалы сохранены — нажмите «Собрать» ещё раз или выберите другую модель."
MSG_TEXT_ONLY = "Скриншоты и другие файлы пока не принимаю — опишите словами, что на них видно, и пришлите текстом."
MSG_BUSY = "⏳ Анализ уже идёт, подождите."


@dataclass
class Outcome:
    kind: str            # 'blocks' | 'single' | 'error' | 'busy' | 'empty'
    blocks: list[str] | None = None
    text: str = ""
    version: int | None = None


async def run_analysis(store: CaseStore, ds: DeepSeekClient, cfg: Config, case_id: int, model: str) -> Outcome:
    """Один запрос на один набор заметок. Состояния и версии — здесь, не в хендлерах."""
    notes = store.joined_notes(case_id)
    if not notes.strip():
        return Outcome("empty", text=HINT_EMPTY)
    if not store.try_start_analysis(case_id):
        return Outcome("busy", text=MSG_BUSY)
    try:
        res = await ds.analyze(notes, model)
        blocks = parse_blocks(res.content)
        if blocks and not has_all_blocks(blocks):  # структура серьёзно нарушена — одна повторная генерация
            res2 = await ds.analyze(notes, model)
            b2 = parse_blocks(res2.content)
            if has_all_blocks(b2):
                res, blocks = res2, b2
    except DeepSeekError as e:
        store.save_analysis(case_id, model, cfg.prompt_version, "error", notes, None)
        store.set_state(case_id, C.FAILED)
        return Outcome("error", text={"fatal": MSG_FATAL, "length": MSG_LENGTH}.get(e.kind, MSG_TEMP))
    except Exception:
        log.exception("unexpected analysis failure")
        store.set_state(case_id, C.FAILED)
        return Outcome("error", text=MSG_TEMP)
    if has_all_blocks(blocks):
        v = store.save_analysis(case_id, model, cfg.prompt_version, "ok", notes, res.content)
        store.set_state(case_id, C.COMPLETED)
        return Outcome("blocks", blocks=[blocks[1], blocks[2], blocks[3]], version=v)
    # «Готов к анализу…» / запрос данных / неполная структура: показать как есть, черновик остаётся открытым
    store.save_analysis(case_id, model, cfg.prompt_version, "partial", notes, res.content)
    store.set_state(case_id, C.DRAFT)
    return Outcome("single", text=res.content)


# --- Telegram ---------------------------------------------------------------

def notes_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} заметка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} заметки"
    return f"{n} заметок"


BTN_NEW, BTN_BUILD, BTN_HISTORY, BTN_CLEAR = "🆕 Новый разбор", "🎬 Собрать сценарий", "📚 История", "🗑 Очистить черновик"
MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_BUILD)],
              [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_CLEAR)]],
    resize_keyboard=True, is_persistent=True)

MSG_NEW_CASE = "Начат новый разбор. Присылайте заметки."
MSG_DONE_HINT = "Этот разбор уже готов. Допишите заметки кнопкой «➕ Дополнить заметки» под результатом или начните новый: 🆕 Новый разбор."


def kb_result(case_id: int) -> InlineKeyboardMarkup:
    """Действия, относящиеся именно к этому результату."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Дополнить заметки", callback_data=f"more:{case_id}")],
        [InlineKeyboardButton(text="🔄 Другая модель", callback_data=f"alt:{case_id}")],
    ])


async def send_formatted(msg: Message, raw: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """HTML; при TelegramBadRequest — чистым текстом. Деление по абзацам до конвертации."""
    parts = split_long(raw, 3500)
    for i, part in enumerate(parts):
        kb = markup if i == len(parts) - 1 else None
        try:
            await msg.answer(md_to_tg_html(part), parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramBadRequest:
            await msg.answer(part, parse_mode=None, reply_markup=kb)


async def send_block(msg: Message, block: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Заголовок блока — отдельным сообщением, тело — отдельным (его удобно копировать целиком)."""
    title, body = split_header(block)
    if title and body:
        await send_formatted(msg, title)
        await send_formatted(msg, body, markup)
    else:
        await send_formatted(msg, block, markup)


async def _typing(msg: Message, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await msg.bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), 4)
        except asyncio.TimeoutError:
            pass


async def safe_answer(cb: CallbackQuery, *args, **kw) -> None:
    """cb.answer() падает с TelegramBadRequest на устаревших кнопках — это не повод бросать обработчик."""
    try:
        await cb.answer(*args, **kw)
    except TelegramBadRequest:
        pass


def add_note_to_case(store: CaseStore, user_id: int, text: str) -> tuple[int, bool]:
    """Добавляет заметку. После завершённого разбора автоматически начинается новый кейс.
    Возвращает (число заметок, начат_ли_новый_кейс)."""
    case = store.current(user_id)
    fresh = case.state == C.COMPLETED
    if fresh:
        case = store.new_case(user_id)
    return store.add_note(case.id, text), fresh


def build_router(cfg: Config, store: CaseStore, ds: DeepSeekClient) -> Router:
    r = Router()

    @r.message(Command("start"))
    async def start(m: Message):
        store.current(m.from_user.id)
        await m.answer("Здравствуйте! Присылайте текстовые наблюдения по карточке (одним или несколькими сообщениями), "
                       "затем нажмите «🎬 Собрать сценарий».\n\n"
                       "Важно: я принимаю только текст. Скриншоты пока не читаю — описывайте словами, что на них.\n\n"
                       "Текст заметок обрабатывается через DeepSeek API.\n\n"
                       "Следующая карточка — просто новое сообщение: после готового разбора я сама начну новый.",
                       reply_markup=MENU)

    async def new_case(m: Message):
        store.new_case(m.from_user.id)
        await m.answer(MSG_NEW_CASE, reply_markup=MENU)

    r.message.register(new_case, Command("new"))
    r.message.register(new_case, F.text == BTN_NEW)

    async def history(m: Message):
        rows = store.history(m.from_user.id)
        if not rows:
            await m.answer("История пока пуста.")
            return
        import datetime as dt
        kb = [[InlineKeyboardButton(
            text=f"{c.title or 'Без названия'} · {dt.datetime.fromtimestamp(c.created_at):%d.%m %H:%M} · {C.STATE_RU[c.state]} · v{n}",
            callback_data=f"hist:{c.id}")] for c, n in rows]
        await m.answer("Последние разборы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

    r.message.register(history, Command("history"))
    r.message.register(history, F.text == BTN_HISTORY)

    @r.callback_query(F.data.startswith("hist:"))
    async def open_hist(cb: CallbackQuery):
        case = store.open_case(cb.from_user.id, int(cb.data.split(":")[1]))
        await safe_answer(cb)
        if not case:
            return
        notes = store.notes(case.id)
        await send_formatted(cb.message, "Заметки:\n\n" + "\n---\n".join(notes))
        last = store.last_ok(case.id)
        if last:
            b = parse_blocks(last["result"])
            for n in (1, 2, 3):
                await send_block(cb.message, b[n], kb_result(case.id) if n == 3 else None)
        else:
            await cb.message.answer("Результата анализа пока нет. Допишите заметки или нажмите «🎬 Собрать сценарий».")

    async def clear(m: Message):
        case = store.current(m.from_user.id)
        if case.state == C.ANALYZING:
            await m.answer(MSG_BUSY)
        elif case.state == C.COMPLETED:  # готовый разбор не стираем
            await new_case(m)
        else:
            store.clear_notes(case.id)
            await m.answer("Черновик очищен.")

    r.message.register(clear, F.text == BTN_CLEAR)

    @r.callback_query(F.data.startswith("more:"))
    async def more(cb: CallbackQuery):
        case = store.open_case(cb.from_user.id, int(cb.data.split(":")[1]))
        await safe_answer(cb)
        if not case or case.state == C.ANALYZING:
            return
        store.set_state(case.id, C.DRAFT)  # явное возвращение в режим сбора
        await cb.message.answer("Пришлите дополнительную деталь — она добавится к заметкам этого разбора, "
                                "затем нажмите «🎬 Собрать сценарий».", reply_markup=MENU)

    async def _build(msg: Message, user_id: int, model: str, allow_completed: bool = False):
        case = store.current(user_id)
        if store.note_count(case.id) == 0:
            await msg.answer(HINT_EMPTY)
            return
        if case.state == C.ANALYZING:
            await msg.answer(MSG_BUSY)
            return
        if case.state == C.COMPLETED and not allow_completed:
            await msg.answer(MSG_DONE_HINT)
            return
        status = await msg.answer("⏳ Анализирую…")
        stop = asyncio.Event()
        typing = asyncio.create_task(_typing(msg, stop))
        try:
            out = await run_analysis(store, ds, cfg, case.id, model)
        finally:
            stop.set()
            await typing
        try:
            await status.delete()
        except Exception:
            pass
        if out.kind == "blocks":
            for i, b in enumerate(out.blocks):
                await send_block(msg, b, kb_result(case.id) if i == 2 else None)
        elif out.kind == "single":
            await send_formatted(msg, out.text)
        else:
            await msg.answer(out.text)

    @r.message(F.text == BTN_BUILD)
    async def build(m: Message):
        await _build(m, m.from_user.id, cfg.model_fast)

    @r.callback_query(F.data.startswith("alt:"))
    async def alt(cb: CallbackQuery):
        case = store.open_case(cb.from_user.id, int(cb.data.split(":")[1]))
        await safe_answer(cb)
        if not case:
            return
        last = store.analyses(case.id)
        used = last[-1]["model"] if last else cfg.model_fast
        model = cfg.model_deep if used == cfg.model_fast else cfg.model_fast
        saved = store.find_ok(case.id, model, store.joined_notes(case.id))
        if saved:  # такой вариант уже есть на тех же заметках — показываем сохранённый, без нового запроса
            await cb.message.answer("Этот вариант уже собран на тех же заметках — показываю сохранённый. "
                                    "Чтобы получить новый, допишите заметки (➕).")
            b = parse_blocks(saved["result"])
            for n in (1, 2, 3):
                await send_block(cb.message, b[n], kb_result(case.id) if n == 3 else None)
            return
        await _build(cb.message, cb.from_user.id, model, allow_completed=True)

    @r.message(F.text & ~F.text.startswith("/"))
    async def note(m: Message):
        if store.current(m.from_user.id).state == C.ANALYZING:
            await m.answer(MSG_BUSY)
            return
        n, fresh = add_note_to_case(store, m.from_user.id, m.text)
        prefix = "Начат новый разбор. " if fresh else ""
        await m.answer(f"{prefix}Добавлено: {notes_word(n)}", reply_markup=MENU)

    @r.message()
    async def other(m: Message):
        caption = (m.caption or "").strip()
        if not caption:
            await m.answer(MSG_TEXT_ONLY)
            return
        if store.current(m.from_user.id).state == C.ANALYZING:
            await m.answer(MSG_BUSY)
            return
        n, fresh = add_note_to_case(store, m.from_user.id, caption)  # файл не читаем, сохраняем только подпись
        prefix = "Начат новый разбор. " if fresh else ""
        await m.answer(f"{prefix}Файл не читаю, но подпись сохранила как заметку. Добавлено: {notes_word(n)}. "
                       "Содержимое скриншота опишите словами.", reply_markup=MENU)

    return r
