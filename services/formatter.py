"""Разбор БЛОК 1/2/3, markdown → Telegram HTML, деление длинного текста."""
from __future__ import annotations

import html
import re

TG_LIMIT = 4096
_HEADER = re.compile(r"^[ \t]*[#*_>\s]*БЛОК[ \t]*([123])\b.*$", re.IGNORECASE | re.MULTILINE)


def parse_blocks(text: str) -> dict[int, str]:
    """Ищет БЛОК 1/2/3 независимо от оформления. Возвращает найденные блоки (может быть пусто)."""
    marks = list(_HEADER.finditer(text or ""))
    blocks: dict[int, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        n = int(m.group(1))
        body = text[m.start():end].strip()
        if n not in blocks:
            blocks[n] = body
    return blocks


def split_header(block: str) -> tuple[str, str]:
    """(заголовок «БЛОК N. …», тело). Заголовок отдельно — чтобы не копировался вместе с текстом."""
    first, _, rest = block.partition("\n")
    if _HEADER.match(first):
        title = re.sub(r"^[#*_>\s]+|[*_\s]+$", "", first)
        return title, rest.strip()
    return "", block


def has_all_blocks(blocks: dict[int, str]) -> bool:
    return all(n in blocks for n in (1, 2, 3))


def md_to_tg_html(text: str) -> str:
    s = html.escape(text, quote=False)  # & < >
    s = re.sub(r"^[ \t]*#{1,6}[ \t]*(.+?)[ \t]*#*[ \t]*$", r"<b>\1</b>", s, flags=re.MULTILINE)
    s = re.sub(r"^[ \t]*(БЛОК[ \t]*[123]\b[^\n<]*)$", r"<b>\1</b>", s, flags=re.MULTILINE)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.DOTALL)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(ЧТО ПОКАЗЫВАТЬ НА ЭКРАНЕ[^\]\n]*)\]", r"<i>[\1]</i>", s)
    return s


def split_long(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Делит по абзацам; слишком длинный абзац — по строкам, затем жёстко."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        for piece in _hard_split(para, limit):
            cand = f"{cur}\n\n{piece}" if cur else piece
            if len(cand) <= limit:
                cur = cand
            else:
                parts.append(cur)
                cur = piece
    if cur:
        parts.append(cur)
    return parts


def _hard_split(para: str, limit: int) -> list[str]:
    if len(para) <= limit:
        return [para]
    out, cur = [], ""
    for line in para.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        cand = f"{cur}\n{line}" if cur else line
        if len(cand) <= limit:
            cur = cand
        else:
            out.append(cur)
            cur = line
    if cur:
        out.append(cur)
    return out
