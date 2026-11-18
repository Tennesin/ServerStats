"""Точечная очистка сообщений: разбор команды -clear, поиск сообщений, удаление, чистка базы статистики."""
import asyncio
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

from config import TZ, fmt_num, log
from db import get_db
from scanner import running

CLEAR_NUM_MAX = 100          # максимум в режиме «число»
CLEAR_RANGE_MAX = 1000       # максимум сообщений за один запуск в режиме after / after+before
CLEAR_SCAN_MAX = 5000        # сколько сообщений максимум просматривать при поиске
BULK_AGE = timedelta(days=14) - timedelta(minutes=5)   # старше — Discord не даёт удалять пачкой
DB_TABLES = ("messages", "mentions", "emoji", "reactions", "reaction_users")

USAGE = (
    "Использование:\n"
    "  -clear ID_сервера ID_канала ЧИСЛО [user ID] [time 2h] [keepdb]\n"
    "      удалить последние ЧИСЛО сообщений (1-100)\n"
    "  -clear ID_сервера ID_канала after ID_сообщения [before ID_сообщения] [user ID] [time 2h] [keepdb] [yes]\n"
    "      удалить всё новее after (и старее before, если задан); сами граничные сообщения не удаляются.\n"
    "      Без yes показывается только предпросмотр.\n"
    "  time: 30m / 2h / 1d / 1w — только сообщения не старше указанного срока.\n"
    "  before без after запрещён."
)

_TIME_RE = re.compile(r"^(\d{1,4})([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

@dataclass
class ClearParams:
    guild_id: int
    channel_id: int
    num: Optional[int] = None
    before_id: Optional[int] = None
    after_id: Optional[int] = None
    user_id: Optional[int] = None
    max_age: Optional[timedelta] = None
    confirm: bool = False
    keep_db: bool = False

def _is_id(tok: str) -> bool:
    return tok.isdigit() and len(tok) >= 17

def parse_clear(args: list) -> ClearParams:
    """Разбор аргументов -clear. Бросает ValueError с понятным текстом."""
    if len(args) < 2 or not (_is_id(args[0]) and _is_id(args[1])):
        raise ValueError("Нужны ID сервера и ID канала.\n" + USAGE)
    p = ClearParams(int(args[0]), int(args[1]))
    it = iter(args[2:])
    for tok in it:
        key = tok.lower()
        if key == "yes":
            p.confirm = True
        elif key == "keepdb":
            p.keep_db = True
        elif key in ("before", "after", "user", "time"):
            val = next(it, None)
            if val is None:
                raise ValueError(f"После «{key}» нужно значение.")
            if key == "time":
                m = _TIME_RE.match(val.lower())
                if not m or int(m.group(1)) < 1:
                    raise ValueError("time: число и единица, например 30m, 2h, 1d, 1w.")
                if p.max_age is not None:
                    raise ValueError("time указан дважды.")
                p.max_age = timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})
            else:
                if not _is_id(val):
                    raise ValueError(f"После «{key}» нужен ID (число из 17+ цифр).")
                field = {"before": "before_id", "after": "after_id", "user": "user_id"}[key]
                if getattr(p, field) is not None:
                    raise ValueError(f"«{key}» указан дважды.")
                setattr(p, field, int(val))
        elif tok.isdigit() and len(tok) < 17:
            if p.num is not None:
                raise ValueError("Число сообщений указано дважды.")
            p.num = int(tok)
        else:
            raise ValueError(f"Не понял аргумент: {tok}")

    if p.num is not None:
        if not 1 <= p.num <= CLEAR_NUM_MAX:
            raise ValueError(f"Число сообщений должно быть от 1 до {CLEAR_NUM_MAX}.")
        if p.before_id or p.after_id:
            raise ValueError("Число нельзя сочетать с before / after: выберите один способ.")
    else:
        if p.after_id is None:
            if p.before_id is not None:
                raise ValueError("«before» без «after» не поддерживается: это удалило бы всю историю "
                                 "канала до сообщения. Укажите ещё и after.")
            raise ValueError("Укажите число сообщений или after.\n" + USAGE)
        if p.before_id is not None and p.after_id >= p.before_id:
            raise ValueError("after должно быть старше before (ID у after меньше). Проверьте порядок.")
    return p

# ---------- Поиск и удаление ----------

async def collect(channel, p: ClearParams, cancelled):
    """Возвращает (найденные сообщения, упёрлись ли в лимит, сколько просмотрено)."""
    cutoff = datetime.now(timezone.utc) - p.max_age if p.max_age else None
    if p.num is not None:                                    # от новых к старым
        it = channel.history(limit=None, oldest_first=False)
        cap = p.num
    else:                                                    # от after к новым; до before остановимся сами
        it = channel.history(limit=None, after=discord.Object(id=p.after_id), oldest_first=True)
        cap = CLEAR_RANGE_MAX

    found, scanned, limit_hit = [], 0, False
    async for m in it:
        if cancelled():
            break
        scanned += 1
        if scanned > CLEAR_SCAN_MAX:
            limit_hit = True
            break
        if p.before_id and m.id >= p.before_id:
            break
        if p.num is not None and cutoff and m.created_at < cutoff:
            break                                            # дальше только более старые
        if cutoff and m.created_at < cutoff:
            continue
        if p.user_id and m.author.id != p.user_id:
            continue
        if m.pinned or m.is_system():
            continue
        if len(found) >= cap:
            limit_hit = True
            break
        found.append(m)
        if p.num is not None and len(found) >= cap:
            break
    return found, limit_hit, scanned

def _snippet(m) -> str:
    text = (m.content or "[без текста]").replace("\n", " ")
    return f"{m.author.display_name}: {text[:60]}"

def preview_lines(found: list, scanned: int, limit_hit: bool, p: ClearParams) -> list:
    now = datetime.now(timezone.utc)
    ordered = sorted(found, key=lambda m: m.id)
    old = sum(1 for m in found if now - m.created_at >= BULK_AGE)
    authors = Counter(m.author.display_name for m in found).most_common(5)
    lines = [
        f"Найдено сообщений: {fmt_num(len(found))} (просмотрено: {fmt_num(scanned)}).",
        f"Самое старое: {ordered[0].created_at.astimezone(TZ):%d.%m.%Y %H:%M} | {_snippet(ordered[0])}",
        f"Самое новое:  {ordered[-1].created_at.astimezone(TZ):%d.%m.%Y %H:%M} | {_snippet(ordered[-1])}",
        "Авторы: " + ", ".join(f"{n} — {c}" for n, c in authors),
    ]
    if old:
        lines.append(f"Старше 14 дней: {old} — Discord не даёт удалять их пачкой, они уходят по одному (медленно).")
    if limit_hit:
        lines.append(f"⚠ Достигнут лимит ({CLEAR_RANGE_MAX} к удалению или {CLEAR_SCAN_MAX} просмотренных): "
                     "будут удалены самые СТАРЫЕ из найденных; остальное повторите отдельной командой.")
    return lines

async def _delete_singly(msgs: list, cancelled, progress_every: int = 50) -> list:
    done = []
    for m in msgs:
        if cancelled():
            break
        try:
            await m.delete()
        except discord.NotFound:
            pass                                             # уже удалено: для базы считаем удалённым
        done.append(m.id)
        if len(done) % progress_every == 0:
            print(f"   … удалено по одному: {len(done)}", flush=True)
    return done

async def delete_found(channel, msgs: list, cancelled) -> list:
    """Удаляет сообщения: свежие пачками по 100, старые по одному. Возвращает ID удалённых."""
    now = datetime.now(timezone.utc)
    fresh = [m for m in msgs if now - m.created_at < BULK_AGE]
    old = [m for m in msgs if now - m.created_at >= BULK_AGE]
    deleted: list = []
    for i in range(0, len(fresh), 100):
        if cancelled():
            return deleted
        chunk = fresh[i:i + 100]
        try:
            await channel.delete_messages(chunk)
            deleted += [m.id for m in chunk]
        except discord.Forbidden:
            raise
        except discord.HTTPException as e:
            log.warning("Пакетное удаление не удалось (HTTP %s), удаляю по одному", e.status)
            deleted += await _delete_singly(chunk, cancelled)
    if old and not cancelled():
        print(f"   Удаляю по одному сообщения старше 14 дней: {len(old)}", flush=True)
        deleted += await _delete_singly(old, cancelled)
    return deleted

def purge_db(ids: list) -> int:
    """Убирает удалённые сообщения из базы статистики. Возвращает, сколько строк было в messages."""
    db = get_db()
    removed = 0
    with db:
        for i in range(0, len(ids), 500):
            part = [(x,) for x in ids[i:i + 500]]
            for table in DB_TABLES:
                cur = db.executemany(f"DELETE FROM {table} WHERE message_id=?", part)
                if table == "messages":
                    removed += cur.rowcount
    return removed

# ---------- Основной сценарий ----------

async def run_clear(bot: discord.Client, p: ClearParams, cancelled):
    guild = bot.get_guild(p.guild_id)
    if guild is None:
        print(f"⚠ Сервер {p.guild_id} не найден среди серверов бота.", flush=True)
        return
    if guild.id in running:
        print(f"⚠ Сервер «{guild.name}» сейчас обрабатывается (/stats или -sync) — подождите.", flush=True)
        return
    channel = guild.get_channel_or_thread(p.channel_id)
    if channel is None or not hasattr(channel, "delete_messages"):
        print(f"⚠ Канал {p.channel_id} не найден на сервере «{guild.name}» "
              "(нужен текстовый, голосовой канал или ветка).", flush=True)
        return

    me = guild.me or await guild.fetch_member(bot.user.id)
    perms = channel.permissions_for(me)
    missing = [name for name, ok in (("Просмотр канала", perms.view_channel),
                                     ("Читать историю сообщений", perms.read_message_history),
                                     ("Управление сообщениями", perms.manage_messages)) if not ok]
    if missing:
        print(f"⚠ У бота в #{channel.name} нет прав: {', '.join(missing)}.", flush=True)
        return

    for label, mid in (("after", p.after_id), ("before", p.before_id)):
        if mid is None:
            continue
        try:
            await channel.fetch_message(mid)
        except discord.NotFound:
            print(f"⚠ Сообщение {label}={mid} не найдено в #{channel.name}. "
                  "Оно должно быть в том же канале, что указан в команде.", flush=True)
            return
        except discord.HTTPException as e:
            print(f"⚠ Не удалось проверить сообщение {label} (HTTP {e.status}).", flush=True)
            return

    print(f"Ищу сообщения в «{guild.name}» -> #{channel.name}…", flush=True)
    found, limit_hit, scanned = await collect(channel, p, cancelled)
    if cancelled():
        print("Остановлено, ничего не удалено.", flush=True)
        return
    if not found:
        print(f"Подходящих сообщений нет (просмотрено: {fmt_num(scanned)}).", flush=True)
        return
    for line in preview_lines(found, scanned, limit_hit, p):
        print("  " + line, flush=True)

    if p.num is None and not p.confirm:
        print("Это предпросмотр, ничего не удалено. Если всё верно, повторите ту же команду с «yes» в конце.",
              flush=True)
        return

    found.sort(key=lambda m: m.id)
    try:
        deleted = await delete_found(channel, found, cancelled)
    except discord.Forbidden:
        print("⚠ Discord отказал в удалении: проверьте право «Управление сообщениями» у бота.", flush=True)
        return

    db_note = ""
    if deleted and not p.keep_db:
        db_removed = await asyncio.to_thread(purge_db, deleted)
        db_note = f"; из базы статистики убрано записей: {fmt_num(db_removed)}"
    stop = " (остановлено вручную)" if cancelled() else ""
    print(f"✔ Удалено сообщений: {fmt_num(len(deleted))} из {fmt_num(len(found))}{stop}{db_note}.", flush=True)