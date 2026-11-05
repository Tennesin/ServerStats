"""Управление из терминала: -stats ID1 ID2 ..., -servers, -cancel, -help."""

import asyncio
import os
import shlex
import sys
import threading
import time
from datetime import datetime
from dataclasses import replace

import discord

from config import (
    DETAIL_LABELS, REACTION_LABELS, SORT_LABELS, STATS_DIR, build_period, collection_dir,
    fmt_duration, fmt_num, log, safe_filename, save_local_copy, DATE_FMT,
)
from reports import build_xlsx, period_text
from scanner import (
    ScanParams, ScanState, background_tasks, merge_results, running, scan_guild,
    guild_start_date, params_for_guild, sync_only,
)

CLI_HELP = """
Команды терминала:
  -stats ID1 ID2 ... [параметры]   — собрать статистику по указанным серверам
  -stats all [параметры]           — по всем серверам, где есть бот
  -sync ID1 ID2 ... [--reactions counts|full|off]  — обновить только базу, без отчётов
  -sync all [--reactions counts|full|off]          — то же по всем серверам
  -servers                         — показать серверы бота и их ID
  -cancel                          — остановить текущий сбор (отчёт будет неполным)
  -help                            — эта справка

Параметры -stats:
  --period ПЕРИОД    пресет: yesterday, this-week, prev-week, this-month, prev-month,
                     this-year, prev-year, last7d, last30d, last3m, last1y …
  --last 45d         то же, что --period last45d (d — дни, w — недели, m — месяцы, y — годы)
  --start дд.мм.гггг, --end дд.мм.гггг   — свои даты (вместо пресета)
  --compare          сравнить с предыдущим периодом такой же длины (читает вдвое больше)
  --exclude "чат1,чат2"   --detail days|weeks|months|auto   --sort count|name
  --reactions counts|full|off   — реакции: счётчики (по умолчанию), полный (кто ставил, медленно), не собирать
  --nosync           построить отчёт только из базы данных, не читая Discord (быстро)
  --live             собрать напрямую из Discord, не используя базу данных (как раньше)
Во всех режимах, включая заданные вручную даты и отсутствие конца, 
учитываются только завершённые сутки (последний день — вчера).
"""

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

async def handle_console_line(bot: discord.Client, line: str):
    try:
        parts = shlex.split(line)
    except ValueError as e:
        print(f"Ошибка разбора команды: {e}", flush=True)
        return
    if not parts:
        return
    cmd, args = parts[0].lower(), parts[1:]

    if cmd == "-help":
        print(CLI_HELP, flush=True)
    elif cmd == "-servers":
        for g in sorted(bot.guilds, key=lambda x: x.name.casefold()):
            members = f", участников: {fmt_num(g.member_count)}" if g.member_count else ""
            print(f"  {g.id}  {g.name}{members}", flush=True)
        print(f"Всего серверов: {len(bot.guilds)}", flush=True)
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
        print(f"Неизвестная команда: {cmd}. Введите -help.", flush=True)

async def console_loop(bot: discord.Client, queue: asyncio.Queue):
    await bot.wait_until_ready()
    print(CLI_HELP, flush=True)
    while True:
        line = await queue.get()
        if line is None:      # stdin закрыт
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