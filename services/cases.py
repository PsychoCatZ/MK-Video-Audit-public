"""Кейсы, заметки и версии анализов в SQLite (переживают перезапуск)."""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

DRAFT, ANALYZING, COMPLETED, FAILED, CANCELLED = "draft", "analyzing", "completed", "failed", "cancelled"
STATE_RU = {DRAFT: "черновик", ANALYZING: "анализ…", COMPLETED: "готов", FAILED: "ошибка", CANCELLED: "отменён"}
NOTE_SEP = "\n---\n"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT,
  state TEXT NOT NULL DEFAULT 'draft', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_cases_user ON cases(user_id, id);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
  text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS analyses(
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
  version INTEGER NOT NULL, model TEXT, prompt_version TEXT, status TEXT NOT NULL,
  notes_snapshot TEXT, result TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS current(user_id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL);
"""


@dataclass
class Case:
    id: int
    user_id: int
    title: str | None
    state: str
    created_at: float
    updated_at: float


class CaseStore:
    def __init__(self, path: str):
        self.path = path
        with closing(self._conn()) as c, c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _run(self, sql: str, args: tuple = ()) -> int:
        with closing(self._conn()) as c, c:
            return c.execute(sql, args).rowcount

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute(sql, args).fetchall()

    # --- старт процесса -------------------------------------------------
    def recover_stuck(self) -> int:
        """Кейсы, оставшиеся в Analyzing после падения процесса, становятся Failed."""
        return self._run("UPDATE cases SET state=?, updated_at=? WHERE state=?", (FAILED, time.time(), ANALYZING))

    # --- кейсы ----------------------------------------------------------
    def _set_current(self, user_id: int, case_id: int) -> None:
        self._run("INSERT INTO current(user_id,case_id) VALUES(?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET case_id=excluded.case_id", (user_id, case_id))

    def new_case(self, user_id: int) -> Case:
        now = time.time()
        with closing(self._conn()) as c, c:
            cid = c.execute("INSERT INTO cases(user_id,state,created_at,updated_at) VALUES(?,?,?,?)",
                            (user_id, DRAFT, now, now)).lastrowid
        self._set_current(user_id, cid)
        return self.get(cid)

    def get(self, case_id: int) -> Case | None:
        r = self._all("SELECT * FROM cases WHERE id=?", (case_id,))
        return Case(**dict(r[0])) if r else None

    def current(self, user_id: int) -> Case:
        """Текущий кейс пользователя; создаётся, если нет."""
        r = self._all("SELECT case_id FROM current WHERE user_id=?", (user_id,))
        if r:
            case = self.get(r[0]["case_id"])
            if case and case.user_id == user_id:
                return case
        return self.new_case(user_id)

    def open_case(self, user_id: int, case_id: int) -> Case | None:
        case = self.get(case_id)
        if not case or case.user_id != user_id:
            return None
        self._set_current(user_id, case_id)
        return case

    def set_state(self, case_id: int, state: str) -> None:
        self._run("UPDATE cases SET state=?, updated_at=? WHERE id=?", (state, time.time(), case_id))

    def try_start_analysis(self, case_id: int) -> bool:
        """Атомарно любое состояние, кроме Analyzing → Analyzing. False при двойном нажатии."""
        return self._run("UPDATE cases SET state=?, updated_at=? WHERE id=? AND state!=?",
                         (ANALYZING, time.time(), case_id, ANALYZING)) == 1

    # --- заметки --------------------------------------------------------
    def add_note(self, case_id: int, text: str) -> int:
        """Добавляет заметку; упавший/отменённый кейс возвращается в Draft. Завершённый остаётся завершённым:
        дописать к нему можно только явно (set_state(DRAFT)), а новые заметки уходят в новый кейс. Возвращает число заметок."""
        now = time.time()
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO notes(case_id,text,created_at) VALUES(?,?,?)", (case_id, text, now))
            c.execute("UPDATE cases SET state=?, updated_at=? WHERE id=? AND state IN (?,?)",
                      (DRAFT, now, case_id, FAILED, CANCELLED))
            c.execute("UPDATE cases SET title=? WHERE id=? AND title IS NULL", (_guess_title(text), case_id))
        return self.note_count(case_id)

    def notes(self, case_id: int) -> list[str]:
        return [r["text"] for r in self._all("SELECT text FROM notes WHERE case_id=? ORDER BY id", (case_id,))]

    def note_count(self, case_id: int) -> int:
        return self._all("SELECT COUNT(*) n FROM notes WHERE case_id=?", (case_id,))[0]["n"]

    def clear_notes(self, case_id: int) -> None:
        self._run("DELETE FROM notes WHERE case_id=?", (case_id,))
        self._run("UPDATE cases SET state=?, title=NULL, updated_at=? WHERE id=?", (DRAFT, time.time(), case_id))

    def joined_notes(self, case_id: int) -> str:
        return NOTE_SEP.join(self.notes(case_id))

    # --- анализы --------------------------------------------------------
    def save_analysis(self, case_id: int, model: str, prompt_version: str, status: str,
                      notes_snapshot: str, result: str | None) -> int:
        """Каждый вызов — новая версия; прежние не затираются."""
        with closing(self._conn()) as c, c:
            v = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM analyses WHERE case_id=?", (case_id,)).fetchone()[0]
            c.execute("INSERT INTO analyses(case_id,version,model,prompt_version,status,notes_snapshot,result,created_at)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (case_id, v, model, prompt_version, status, notes_snapshot, result, time.time()))
        return v

    def analyses(self, case_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM analyses WHERE case_id=? ORDER BY version", (case_id,))

    def last_ok(self, case_id: int) -> sqlite3.Row | None:
        r = self._all("SELECT * FROM analyses WHERE case_id=? AND status='ok' ORDER BY version DESC LIMIT 1", (case_id,))
        return r[0] if r else None

    def find_ok(self, case_id: int, model: str, notes: str) -> sqlite3.Row | None:
        """Уже готовый результат этой модели на точно таких же заметках (повторный запрос не нужен)."""
        r = self._all("SELECT * FROM analyses WHERE case_id=? AND status='ok' AND model=? AND notes_snapshot=? "
                      "ORDER BY version DESC LIMIT 1", (case_id, model, notes))
        return r[0] if r else None

    def history(self, user_id: int, limit: int = 8) -> list[tuple[Case, int]]:
        """Последние кейсы с заметками: (кейс, число успешных версий анализа)."""
        rows = self._all("SELECT * FROM cases WHERE user_id=? AND EXISTS(SELECT 1 FROM notes WHERE case_id=cases.id) "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit))
        out = []
        for r in rows:
            n = self._all("SELECT COUNT(*) n FROM analyses WHERE case_id=? AND status='ok'", (r["id"],))[0]["n"]
            out.append((Case(**dict(r)), n))
        return out


def _guess_title(text: str) -> str:
    m = re.search(r"\b\d{6,12}\b", text)  # артикул WB/Ozon
    if m:
        return m.group(0)
    lines = text.strip().splitlines()
    return (lines[0] if lines else "")[:40]
