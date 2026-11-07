"""Управление из терминала: -stats, -sync, -servers, -cancel.
Справка — в файле, который создаёт help_gen.py."""

import asyncio
import os
import shlex
import sys
import threading
import time
from datetime import datetime
from dataclasses import replace
import re
import discord
import emoji as emoji_lib

from config import (
    DETAIL_LABELS, REACTION_LABELS, SORT_LABELS, STATS_DIR, build_period, collection_dir,
    fmt_duration, fmt_num, log, safe_filename, save_local_copy, DATE_FMT,
)
from reports import build_xlsx, period_text
from scanner import (
    ScanParams, ScanState, background_tasks, merge_results, running, scan_guild,
    guild_start_date, params_for_guild, sync_only,
)

VALUE_OPTS = {"period", "last", "start", "end", "exclude", "detail", "sort", "reactions"}
FLAG_OPTS = {"compare", "nosync", "live"}

_cli = {"busy": False, "cancel": False}

def parse_cli_stats(args: list[str]):
    """Возвращает (ids, all_guilds, params). Бросает ValueError."""
    ids: list[int] = []
    all_guilds = False
    opts = {"period": None, "last": None, "start": None, "end": None,
            "exclude": "", "detail": "auto", "sort": "count", "reactions": "counts"}
    flags: set[str] = set()
    it = iter(args)
    for a in it:
        if a.startswith("--"):
            key = a[2:]
            if key in FLAG_OPTS:
                flags.add(key)
                continue
            if key not in VALUE_OPTS:
                raise ValueError(f"Неизвестный параметр: {a}")
            val = next(it, None)
            if val is None:
                raise ValueError(f"У параметра {a} нет значения")
            opts[key] = val
            continue
        for tok in a.replace(",", " ").split():
            tok = tok.strip("[]")
            if tok.lower() == "all":
                all_guilds = True
            elif tok.isdigit():
                ids.append(int(tok))
            elif tok:
                raise ValueError(f"Не понял аргумент: {tok}")

    if "live" in flags and "nosync" in flags:
        raise ValueError("--live и --nosync несовместимы: --live не использует базу вовсе.")
    compare = "compare" in flags

    if opts["last"]:
        if opts["period"]:
            raise ValueError("Укажите либо --period, либо --last.")
        last = opts["last"].strip().lower()
        opts["period"] = last if last.startswith("last") else "last" + last
    if opts["detail"] not in DETAIL_LABELS:
        raise ValueError(f"--detail: одно из {', '.join(DETAIL_LABELS)}")
    if opts["sort"] not in SORT_LABELS:
        raise ValueError(f"--sort: одно из {', '.join(SORT_LABELS)}")
    opts["reactions"] = str(opts["reactions"]).strip().lower()
    if opts["reactions"] not in REACTION_LABELS:
        raise ValueError(f"--reactions: одно из {', '.join(REACTION_LABELS)}")

    s, e, key, label = build_period(opts["period"], opts["start"], opts["end"], compare)
    params = ScanParams(
        start_date=s, end_date=e, user=None, channel=None,
        sort_key=opts["sort"], detail=opts["detail"], fmt="xlsx", exclude=opts["exclude"],
        period_key=key, period_label=label, compare=compare, reactions=opts["reactions"],
        sync="nosync" not in flags, live="live" in flags,
    )
    return ids, all_guilds, params

def parse_cli_sync(args: list[str]):
    """Возвращает (ids, all_guilds, reactions). Бросает ValueError."""
    ids: list[int] = []
    all_guilds = False
    reactions = "counts"
    it = iter(args)
    for a in it:
        if a == "--reactions":
            val = next(it, None)
            if val is None:
                raise ValueError("У параметра --reactions нет значения")
            reactions = val.strip().lower()
            continue
        if a.startswith("--"):
            raise ValueError(f"Неизвестный параметр: {a}")
        for tok in a.replace(",", " ").split():
            tok = tok.strip("[]")
            if tok.lower() == "all":
                all_guilds = True
            elif tok.isdigit():
                ids.append(int(tok))
            elif tok:
                raise ValueError(f"Не понял аргумент: {tok}")
    if reactions not in REACTION_LABELS:
        raise ValueError(f"--reactions: одно из {', '.join(REACTION_LABELS)}")
    return ids, all_guilds, reactions

async def run_sync_batch(bot: discord.Client, guilds: list[discord.Guild], reactions: str):
    _cli["busy"], _cli["cancel"] = True, False
    started = time.monotonic()
    try:
        for i, g in enumerate(guilds, start=1):
            if _cli["cancel"]:
                break
            if g.id in running:
                print(f"⚠ Сервер «{g.name}» уже обрабатывается (/stats или /sync) — пропускаю.", flush=True)
                continue
            print(f"▶ [{i}/{len(guilds)}] Обновляю базу: «{g.name}» ({g.id})", flush=True)
            state = ScanState(title=g.name)
            running[g.id] = state
            try:
                await sync_only(g, reactions, bot.user.id, state)
            finally:
                state.done = True
                running.pop(g.id, None)
            flag = " (остановлено, данные частичные)" if state.cancelled else ""
            print(f"✔ «{g.name}»: просмотрено {fmt_num(state.seen)}, обработано {fmt_num(state.counted)}, "
                  f"каналов {state.channels_done}/{state.total_channels}{flag}", flush=True)
            for n in state.notes[:5]:
                print(f"   • {n}", flush=True)
        print(f"✅ Обновление базы завершено за {fmt_duration(time.monotonic() - started)}.", flush=True)
    except Exception:
        log.exception("Обновление базы завершилось ошибкой")
    finally:
        _cli["busy"] = False

async def run_batch(bot: discord.Client, guilds: list[discord.Guild], params: ScanParams):
    _cli["busy"], _cli["cancel"] = True, False
    started = time.monotonic()
    folder = collection_dir(params.start_date, params.end_date, STATS_DIR, unique=True,
                            full_history=params.full_history)
    results = []
    used_params = []
    try:
        for i, g in enumerate(guilds, start=1):
            if _cli["cancel"]:
                break
            if g.id in running:
                print(f"⚠ Сервер «{g.name}» уже сканируется через /stats — пропускаю.", flush=True)
                continue
            state = ScanState(title=g.name)
            g_params = params_for_guild(g, params, state)
            if g_params is None:
                print(f"⚠ Сервер «{g.name}»: в выбранном периоде у него нет истории "
                      f"(начало — {guild_start_date(g).strftime(DATE_FMT)}) — пропускаю.", flush=True)
                continue
            print(f"▶ [{i}/{len(guilds)}] Сервер «{g.name}» ({g.id})", flush=True)
            if g_params.start_date != params.start_date:
                print(f"  Период сокращён до начала истории сервера: с "
                      f"{g_params.start_date.strftime(DATE_FMT)}", flush=True)
            running[g.id] = state
            try:
                result = await scan_guild(g, g_params, bot.user.id, state)
            finally:
                state.done = True
                running.pop(g.id, None)
            data = await asyncio.to_thread(build_xlsx, g.name, g_params, state, result)
            fname = f"{safe_filename(g.name)} ({g.id}).xlsx"
            await asyncio.to_thread(save_local_copy, fname, data, folder)
            results.append((g, state, result))
            used_params.append(g_params)
            print(f"✔ Сохранено: {fname} (учтено сообщений: {fmt_num(state.counted)})", flush=True)

        if len(results) > 1:
            merged, total = merge_results(results)
            m_params = replace(
                params,
                start_date=min(p.start_date for p in used_params),   # объединение историй
                compare=merged.prev is not None,
            )
            pseudo_name = f"Суммарно по серверам: {len(results)}"
            data = await asyncio.to_thread(build_xlsx, pseudo_name, m_params, total, merged)
            await asyncio.to_thread(save_local_copy, "Суммарная статистика.xlsx", data, folder)
            print("✔ Сохранено: Суммарная статистика.xlsx", flush=True)

        if results:
            print(f"✅ Готово за {fmt_duration(time.monotonic() - started)}. "
                  f"Сохранено в: {folder}", flush=True)
        else:
            print("Нечего сохранять: ни один сервер не был обработан.", flush=True)
    except Exception:
        log.exception("Пакетный сбор завершился ошибкой")
    finally:
        _cli["busy"] = False

SAY_MAX = 2000
_MENTION_RE = re.compile(r"(?<![<\w])@(?:\{([^}]+)\}|([\w.\-]+))")

def resolve_mentions(guild: discord.Guild, text: str):
    """@имя / @{имя с пробелами} / @ID -> <@id> (роли -> <@&id>). Возвращает (текст, список ошибок)."""
    errors: list[str] = []

    def repl(m):
        raw = m.group(1) or m.group(2)
        low = raw.casefold()
        if low in ("everyone", "here"):
            return m.group(0)                      # остаётся обычным текстом, пинга не будет
        if raw.isdigit() and len(raw) >= 17:
            return f"<@{raw}>"
        members = [mb for mb in guild.members
                   if low in {mb.name.casefold(), mb.display_name.casefold(),
                              (mb.global_name or "").casefold()}]
        if len(members) == 1:
            return f"<@{members[0].id}>"
        if len(members) > 1:
            variants = ", ".join(f"{mb.display_name} ({mb.id})" for mb in members[:5])
            errors.append(f"@{raw}: подходит несколько человек: {variants}. Укажите ID: @ID")
            return m.group(0)
        roles = [r for r in guild.roles if not r.is_default() and r.name.casefold() == low]
        if len(roles) == 1:
            return f"<@&{roles[0].id}>"
        if len(roles) > 1:
            errors.append(f"@{raw}: ролей с таким названием несколько.")
            return m.group(0)
        errors.append(f"@{raw}: никого не найдено (проверьте написание или укажите ID).")
        return m.group(0)

    return _MENTION_RE.sub(repl, text), errors

_EMOJI_RE = re.compile(r"(?<![<\w]):([A-Za-z0-9_]{2,32}):")

def resolve_emoji(guild: discord.Guild, text: str) -> str:
    """:имя: -> кастомный эмодзи сервера (<:имя:id>) или Unicode-эмодзи по короткому имени.
    Готовые записи <:имя:id> и неизвестные :имена: остаются без изменений."""
    own = {}
    for e in guild.emojis:
        own.setdefault(e.name.casefold(), e)

    def repl(m):
        e = own.get(m.group(1).casefold())
        if e is not None:
            return str(e)                              # <:имя:id> или <a:имя:id>
        return emoji_lib.emojize(m.group(0), language="alias")   # не нашли — остаётся как было

    return _EMOJI_RE.sub(repl, text)

async def say(bot: discord.Client, line: str):
    """-say ID_канала [--reply ID_сообщения] [--noping] текст: отправляет сообщение от имени бота."""
    usage = ("Использование: -say ID_канала [--reply ID_сообщения] [--noping] текст сообщения   "
             "(теги: @имя или @{имя с пробелами})")
    parts = line.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        print(usage, flush=True)
        return
    cid, rest = int(parts[1]), parts[2]

    # Необязательные параметры перед текстом
    reply_id, ping = None, True
    while rest.startswith("--"):
        head, _, tail = rest.partition(" ")
        name = head.lower()
        if name == "--reply":
            num, _, tail = tail.lstrip().partition(" ")
            if not num.isdigit():
                print("⚠ После --reply нужен ID сообщения (число).", flush=True)
                return
            reply_id = int(num)
        elif name == "--noping":
            ping = False
        else:
            print(f"⚠ Неизвестный параметр {head}. {usage}", flush=True)
            return
        rest = tail.lstrip()
    text = rest.replace("\\n", "\n")
    if not text.strip():
        print(usage, flush=True)
        return

    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except discord.HTTPException:
            print(f"⚠ Канал {cid} не найден или нет доступа.", flush=True)
            return
    if not hasattr(channel, "send") or getattr(channel, "guild", None) is None:
        print(f"⚠ В «{channel}» нельзя отправлять сообщения (нужен текстовый канал или ветка сервера).",
              flush=True)
        return

    guild = channel.guild
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            print(f"⚠ Не удалось полностью загрузить список участников ({e}): теги по имени могут не найтись.",
                  flush=True)

    text, errors = resolve_mentions(guild, text)
    if errors:
        print("Сообщение НЕ отправлено:", flush=True)
        for e in errors:
            print(f"   • {e}", flush=True)
        return
    text = resolve_emoji(guild, text)
    if len(text) > SAY_MAX:
        print(f"⚠ Сообщение слишком длинное: {len(text)} из {SAY_MAX} символов.", flush=True)
        return

    # Сообщение, на которое отвечаем (ищем в том же канале)
    target = None
    if reply_id is not None:
        try:
            target = await channel.fetch_message(reply_id)
        except discord.NotFound:
            print(f"⚠ Сообщение {reply_id} не найдено в #{getattr(channel, 'name', cid)}. "
                  "Оно должно быть в том же канале, что указан в команде.", flush=True)
            return
        except discord.Forbidden:
            print("⚠ Нет доступа к истории канала: боту нужно «Читать историю сообщений».", flush=True)
            return
        except discord.HTTPException as e:
            print(f"⚠ Не удалось получить сообщение (HTTP {e.status}): {e.text}", flush=True)
            return

    try:
        sent = await channel.send(
            text,
            reference=target,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=True, everyone=False, replied_user=ping))
        extra = f", ответ на сообщение {reply_id} ({target.author.display_name})" if target else ""
        print(f"✔ Отправлено в «{guild.name}» -> #{getattr(channel, 'name', cid)} "
              f"(id сообщения {sent.id}{extra}).", flush=True)
    except discord.Forbidden:
        print("⚠ Нет прав: боту нужно «Отправлять сообщения» (и «Отправлять сообщения в ветках» для веток).",
              flush=True)
    except discord.HTTPException as e:
        print(f"⚠ Discord отклонил сообщение (HTTP {e.status}): {e.text}", flush=True)

async def handle_console_line(bot: discord.Client, line: str):
    if line.split(None, 1)[0].lower() == "-say":
        await say(bot, line)
        return
    try:
        parts = shlex.split(line)
    except ValueError as e:
        print(f"Ошибка разбора команды: {e}", flush=True)
        return
    if not parts:
        return
    cmd, args = parts[0].lower(), parts[1:]

    if cmd == "-servers":
        for g in sorted(bot.guilds, key=lambda x: x.name.casefold()):
            members = f", участников: {fmt_num(g.member_count)}" if g.member_count else ""
            print(f"  {g.id}  {g.name}{members}", flush=True)
        print(f"Всего серверов: {len(bot.guilds)}", flush=True)
    elif cmd == "-emojis":
        if not args or not args[0].isdigit():
            print("Использование: -emojis ID_сервера [часть названия]", flush=True)
            return
        g = bot.get_guild(int(args[0]))
        if g is None:
            print(f"⚠ Сервер {args[0]} не найден среди серверов бота.", flush=True)
            return
        part = args[1].casefold() if len(args) > 1 else ""
        found = sorted((e for e in g.emojis if part in e.name.casefold()),
                       key=lambda x: x.name.casefold())
        for e in found:
            kind = " (анимированный)" if e.animated else ""
            print(f"  {e.id}  {e.name}{kind}  ->  {e}", flush=True)
        print(f"Эмодзи: {len(found)} из {len(g.emojis)}", flush=True)
    elif cmd == "-cancel":
        _cli["cancel"] = True
        for st in running.values():
            st.cancelled = True
        print("Останавливаю… Сохраню то, что успел собрать.", flush=True)
    elif cmd == "-sync":
        if _cli["busy"]:
            print("Пакетная операция уже идёт. Дождитесь окончания или введите -cancel.", flush=True)
            return
        try:
            ids, all_guilds, reactions = parse_cli_sync(args)
        except ValueError as e:
            print(f"Ошибка: {e}", flush=True)
            return
        if all_guilds:
            guilds = list(bot.guilds)
        else:
            guilds, seen_ids = [], set()
            for gid in ids:
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                g = bot.get_guild(gid)
                if g is None:
                    print(f"⚠ Сервер {gid} не найден среди серверов бота — пропускаю.", flush=True)
                else:
                    guilds.append(g)
        if not guilds:
            print("Не указано ни одного доступного сервера. Пример: -sync all", flush=True)
            return
        task = asyncio.create_task(run_sync_batch(bot, guilds, reactions))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    elif cmd == "-stats":
        if _cli["busy"]:
            print("Пакетный сбор уже идёт. Дождитесь окончания или введите -cancel.", flush=True)
            return
        try:
            ids, all_guilds, params = parse_cli_stats(args)
        except ValueError as e:
            print(f"Ошибка: {e}", flush=True)
            return
        if all_guilds:
            guilds = list(bot.guilds)
        else:
            guilds, seen_ids = [], set()
            for gid in ids:
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                g = bot.get_guild(gid)
                if g is None:
                    print(f"⚠ Сервер {gid} не найден среди серверов бота — пропускаю.", flush=True)
                else:
                    guilds.append(g)
        if not guilds:
            print("Не указано ни одного доступного сервера. Пример: -stats 123456789012345678", flush=True)
            return
        print(f"Период: {period_text(params)}"
              + (" • со сравнением" if params.compare else ""), flush=True)
        task = asyncio.create_task(run_batch(bot, guilds, params))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    else:
        print(
            f"Неизвестная команда: {cmd}. "
            f"Список команд — в файле «Команды бота.txt» (создаётся запуском help_gen.py).",
            flush=True)

async def console_loop(bot: discord.Client, queue: asyncio.Queue):
    await bot.wait_until_ready()
    while True:
        line = await queue.get()
        if line is None:
            return
        if line.strip():
            await handle_console_line(bot, line.strip())

def start_console(bot: discord.Client):
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def reader():
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()
    task = asyncio.create_task(console_loop(bot, queue))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)