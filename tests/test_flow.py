import asyncio
from pathlib import Path

import pytest

from config import load_config
from handlers.audit_flow import run_analysis
from services import cases as C
from services.cases import CaseStore
from services.deepseek_client import DeepSeekError
from services.formatter import md_to_tg_html, parse_blocks, split_long

THREE = "## БЛОК 1. Сообщение\nТекст 1\n\n**БЛОК 2** план\n0-3 c\n\nБЛОК 3: саммари\nИтог"
NEED = "Готов к анализу. Пришлите, пожалуйста, цену и категорию."


class FakeDS:
    def __init__(self, *replies, delay=0):
        self.replies, self.calls, self.delay = list(replies), [], delay

    async def analyze(self, notes, model):
        self.calls.append((notes, model))
        if self.delay:
            await asyncio.sleep(self.delay)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return type("R", (), {"content": r, "model": model, "seconds": 0})()


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "demo_video_audit_v1.0.md"
    p.write_text("prompt", encoding="utf-8")
    return load_config({"BOT_TOKEN": "t", "DEEPSEEK_API_KEY": "k", "ALLOWED_USER_IDS": "1,2",
                        "MODEL_FAST": "fast", "MODEL_DEEP": "deep", "PROMPT_PATH": str(p),
                        "DB_PATH": str(tmp_path / "a.db")})


@pytest.fixture
def store(cfg):
    return CaseStore(cfg.db_path)


def test_parser_variants_and_missing():
    b = parse_blocks(THREE)
    assert sorted(b) == [1, 2, 3] and "Текст 1" in b[1] and "0-3" in b[2]
    assert parse_blocks(NEED) == {}
    assert sorted(parse_blocks("БЛОК 1\nx\nБЛОК 3\ny")) == [1, 3]  # без IndexError


def test_md_html_and_split():
    assert md_to_tg_html("a<b & **x**") == "a&lt;b &amp; <b>x</b>"
    assert "<i>[ЧТО ПОКАЗЫВАТЬ НА ЭКРАНЕ: карточка]</i>" in md_to_tg_html("[ЧТО ПОКАЗЫВАТЬ НА ЭКРАНЕ: карточка]")
    parts = split_long("\n\n".join(["x" * 1000] * 10), 3500)
    assert all(len(p) <= 3500 for p in parts) and len(parts) > 1
    assert all(len(p) <= 100 for p in split_long("y" * 450, 100))


async def test_empty_input_no_api_call(store, cfg):
    ds = FakeDS(THREE)
    case = store.new_case(1)
    out = await run_analysis(store, ds, cfg, case.id, "fast")
    assert out.kind == "empty" and ds.calls == []


async def test_multiple_notes_one_case_one_request(store, cfg):
    ds = FakeDS(THREE)
    case = store.current(1)
    store.add_note(case.id, "артикул 123456789")
    store.add_note(case.id, "фото плохие")
    assert store.current(1).id == case.id
    out = await run_analysis(store, ds, cfg, case.id, "fast")
    assert out.kind == "blocks" and len(out.blocks) == 3
    assert ds.calls[0][0] == "артикул 123456789\n---\nфото плохие"
    assert store.get(case.id).state == C.COMPLETED and store.get(case.id).title == "123456789"


async def test_insufficient_data_single_message_draft_open(store, cfg):
    ds = FakeDS(NEED)
    case = store.current(1)
    store.add_note(case.id, "мало")
    out = await run_analysis(store, ds, cfg, case.id, "fast")
    assert out.kind == "single" and out.text == NEED and len(ds.calls) == 1
    assert store.get(case.id).state == C.DRAFT and store.notes(case.id) == ["мало"]


async def test_partial_structure_regenerates_once(store, cfg):
    ds = FakeDS("БЛОК 1\nx", THREE)
    case = store.current(1)
    store.add_note(case.id, "n")
    out = await run_analysis(store, ds, cfg, case.id, "fast")
    assert out.kind == "blocks" and len(ds.calls) == 2


async def test_deepseek_error_keeps_notes(store, cfg):
    ds = FakeDS(DeepSeekError("temporary"))
    case = store.current(1)
    store.add_note(case.id, "заметка")
    out = await run_analysis(store, ds, cfg, case.id, "fast")
    assert out.kind == "error" and "сохранены" in out.text
    assert store.notes(case.id) == ["заметка"] and store.get(case.id).state == C.FAILED
    ds2 = FakeDS(THREE)  # повтор возможен
    assert (await run_analysis(store, ds2, cfg, case.id, "fast")).kind == "blocks"


async def test_double_click_single_request(store, cfg):
    ds = FakeDS(THREE, delay=0.1)
    case = store.current(1)
    store.add_note(case.id, "n")
    a, b = await asyncio.gather(run_analysis(store, ds, cfg, case.id, "fast"),
                                run_analysis(store, ds, cfg, case.id, "fast"))
    assert sorted([a.kind, b.kind]) == ["blocks", "busy"] and len(ds.calls) == 1


def test_stuck_analyzing_becomes_failed_and_draft_survives_restart(store, cfg):
    case = store.current(1)
    store.add_note(case.id, "n1")
    assert store.try_start_analysis(case.id)
    store2 = CaseStore(cfg.db_path)  # «перезапуск»
    assert store2.recover_stuck() == 1
    assert store2.get(case.id).state == C.FAILED
    assert store2.current(1).id == case.id and store2.notes(case.id) == ["n1"]


def test_new_case_inherits_nothing_and_users_isolated(store):
    c1 = store.current(1)
    store.add_note(c1.id, "старое")
    c2 = store.new_case(1)
    assert c2.id != c1.id and store.notes(c2.id) == []
    u2 = store.current(2)
    store.add_note(u2.id, "чужое")
    assert store.notes(store.current(1).id) == [] and store.notes(u2.id) == ["чужое"]
    assert store.open_case(2, c1.id) is None  # чужой кейс не открыть


async def test_reanalysis_keeps_old_version_and_supplement(store, cfg):
    ds = FakeDS(THREE)
    case = store.current(1)
    store.add_note(case.id, "a")
    await run_analysis(store, ds, cfg, case.id, "fast")
    store.set_state(case.id, C.DRAFT)  # кнопка «Дополнить заметки»
    store.add_note(case.id, "доп")
    assert store.get(case.id).state == C.DRAFT
    await run_analysis(store, ds, cfg, case.id, "deep")
    vs = store.analyses(case.id)
    assert [v["version"] for v in vs] == [1, 2] and vs[0]["result"] and vs[1]["model"] == "deep"
    assert vs[0]["prompt_version"] == "v1.0"
    assert store.history(1)[0][1] == 2


async def test_length_error_has_own_message(store, cfg):
    ds = FakeDS(DeepSeekError("length"))
    case = store.current(1)
    store.add_note(case.id, "n")
    out = await run_analysis(store, ds, cfg, case.id, "fast")
    assert out.kind == "error" and "лимит" in out.text and store.get(case.id).state == C.FAILED


def test_block_header_bold_and_plural():
    from handlers.audit_flow import notes_word
    assert md_to_tg_html("БЛОК 1. Первое сообщение\nтекст") == "<b>БЛОК 1. Первое сообщение</b>\nтекст"
    assert [notes_word(n) for n in (1, 2, 5, 11, 21, 22)] == ["1 заметка", "2 заметки", "5 заметок", "11 заметок", "21 заметка", "22 заметки"]


def test_split_header():
    from services.formatter import split_header
    assert split_header("## БЛОК 1. Первое сообщение\n\nТекст") == ("БЛОК 1. Первое сообщение", "Текст")
    assert split_header("просто текст") == ("", "просто текст")
    t, b = split_header(parse_blocks(THREE)[2])
    assert t.startswith("БЛОК 2") and "БЛОК" not in b and "0-3" in b


async def test_two_cards_in_a_row_without_new(store, cfg):
    from handlers.audit_flow import add_note_to_case
    ds = FakeDS(THREE)
    n, fresh = add_note_to_case(store, 1, "карточка 111111111, мелкий текст")
    assert (n, fresh) == (1, False)
    c1 = store.current(1)
    await run_analysis(store, ds, cfg, c1.id, "fast")
    assert store.get(c1.id).state == C.COMPLETED
    n, fresh = add_note_to_case(store, 1, "карточка 222222222, другое")  # без /new
    c2 = store.current(1)
    assert fresh and n == 1 and c2.id != c1.id
    assert store.notes(c2.id) == ["карточка 222222222, другое"] and store.notes(c1.id) == ["карточка 111111111, мелкий текст"]
    await run_analysis(store, ds, cfg, c2.id, "fast")
    assert "111111111" not in ds.calls[1][0] and "222222222" in ds.calls[1][0]


async def test_incomplete_result_keeps_case_open_for_more_notes(store, cfg):
    from handlers.audit_flow import add_note_to_case
    ds = FakeDS(NEED)
    add_note_to_case(store, 1, "всё плохо")
    c = store.current(1)
    await run_analysis(store, ds, cfg, c.id, "fast")
    n, fresh = add_note_to_case(store, 1, "артикул 123456789, мелкий текст")
    assert not fresh and n == 2 and store.current(1).id == c.id


async def test_find_ok_reuses_same_model_same_notes(store, cfg):
    ds = FakeDS(THREE)
    c = store.current(1)
    store.add_note(c.id, "a")
    await run_analysis(store, ds, cfg, c.id, "fast")
    await run_analysis(store, ds, cfg, c.id, "deep")
    notes = store.joined_notes(c.id)
    assert store.find_ok(c.id, "fast", notes) and store.find_ok(c.id, "deep", notes)
    assert store.find_ok(c.id, "fast", notes + "\n---\nещё") is None  # заметки изменились — нужен новый запрос
