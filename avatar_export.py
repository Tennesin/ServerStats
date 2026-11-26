"""Выгрузка аватарок участников сервера: круглые PNG с прозрачными углами."""
import asyncio
import io
import os
import time
from typing import Optional

import discord
from PIL import Image, ImageChops, ImageDraw

from config import STATS_DIR, fmt_duration, fmt_num, log, safe_filename
from avatar_export import USAGE as AVATAR_USAGE, parse_avatar, run_avatar

AVATAR_SIZE = 512          # итоговая сторона картинки, px
MASK_SS = 4                # во сколько раз крупнее рисуется маска круга (сглаживание краёв)
DOWNLOAD_CONCURRENCY = 8   # сколько аватарок качать одновременно
PROGRESS_EVERY = 50

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

USAGE = (
    "Использование:\n"
    "  -avatar ID_сервера [all | ник | ID_пользователя] [--bots]\n"
    "      all (по умолчанию) — аватарки всех участников сервера (боты пропускаются);\n"
    "      ник — точное имя (ник на сервере, логин или глобальное имя; с пробелами — в кавычках);\n"
    "      ID_пользователя — 17+ цифр (можно и для человека, которого уже нет на сервере);\n"
    "      --bots — при all включить и ботов.\n"
    "  Результат: папка avatars/ рядом с отчётами; PNG 512x512, круглые, с прозрачными углами."
)

def parse_avatar(args: list):
    """Возвращает (ID сервера, запрос или None, включать ли ботов). Бросает ValueError."""
    if not args or not (args[0].isdigit() and len(args[0]) >= 17):
        raise ValueError("Нужен ID сервера.\n" + USAGE)
    include_bots = False
    rest = []
    for a in args[1:]:
        if a.lower() == "--bots":
            include_bots = True
        elif a.startswith("--"):
            raise ValueError(f"Неизвестный параметр: {a}")
        else:
            rest.append(a)
    query = " ".join(rest).strip() or None
    return int(args[0]), query, include_bots

def avatars_dir(guild: discord.Guild) -> str:
    """Папка аватарок: рядом с отчётами (STATS_DIR или output), подпапка на сервер."""
    base = STATS_DIR or "output"
    return os.path.abspath(os.path.join(base, "avatars", f"{safe_filename(guild.name)} ({guild.id})"))

def make_round_png(data: bytes) -> bytes:
    """Квадратная обрезка по центру, масштаб до AVATAR_SIZE, круглая маска со сглаживанием.
    Вне круга пиксели полностью прозрачные."""
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    im = im.crop((left, top, left + side, top + side)).resize((AVATAR_SIZE, AVATAR_SIZE), RESAMPLE)

    big = AVATAR_SIZE * MASK_SS
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((AVATAR_SIZE, AVATAR_SIZE), RESAMPLE)

    im.putalpha(ImageChops.multiply(im.getchannel("A"), mask))   # учитываем и родную прозрачность аватарки
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()

def _save(raw: bytes, path: str):
    png = make_round_png(raw)
    with open(path, "wb") as f:
        f.write(png)

async def resolve_targets(bot: discord.Client, guild: discord.Guild, query: Optional[str],
                          include_bots: bool):
    """Возвращает (список Member/User, текст ошибки или None)."""
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            print(f"⚠ Не удалось полностью загрузить список участников ({e}): "
                  "результат может быть неполным.", flush=True)

    if query is None or query.lower() == "all":
        return [m for m in guild.members if include_bots or not m.bot], None

    if query.isdigit() and len(query) >= 17:
        uid = int(query)
        member = guild.get_member(uid)
        if member is not None:
            return [member], None
        try:                                           # человека уже нет на сервере: берём глобальный профиль
            return [await bot.fetch_user(uid)], None
        except discord.HTTPException:
            return [], f"Пользователь с ID {uid} не найден."

    low = query.casefold().lstrip("@")
    found = [m for m in guild.members
             if low in {m.name.casefold(), m.display_name.casefold(), (m.global_name or "").casefold()}]
    if not found:
        return [], f"Никого с ником «{query}» не найдено (проверьте написание или укажите ID)."
    if len(found) > 1:
        variants = ", ".join(f"{m.display_name} ({m.id})" for m in found[:5])
        return [], f"Подходит несколько человек: {variants}. Укажите ID."
    return found, None

async def run_avatar(bot: discord.Client, guild_id: int, query: Optional[str],
                     include_bots: bool, cancelled):
    guild = bot.get_guild(guild_id)
    if guild is None:
        print(f"⚠ Сервер {guild_id} не найден среди серверов бота.", flush=True)
        return

    targets, error = await resolve_targets(bot, guild, query, include_bots)
    if error:
        print(f"⚠ {error}", flush=True)
        return
    if not targets:
        print("Некого выгружать: список участников пуст.", flush=True)
        return

    folder = avatars_dir(guild)
    os.makedirs(folder, exist_ok=True)
    print(f"▶ «{guild.name}»: аватарок к выгрузке {fmt_num(len(targets))} -> {folder}", flush=True)

    started = time.monotonic()
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    done = ok = 0
    failed: list = []

    async def one(user):
        nonlocal done, ok
        async with sem:
            if cancelled():
                return
            name = user.display_name
            try:
                asset = user.display_avatar.replace(size=AVATAR_SIZE, format="png")
                raw = await asset.read()
                fname = f"{safe_filename(name)} ({user.id}).png"
                await asyncio.to_thread(_save, raw, os.path.join(folder, fname))
                ok += 1
            except Exception as e:
                log.warning("Аватарка %s (%s) не выгружена: %s", name, user.id, e)
                failed.append(f"{name} ({user.id})")
            finally:
                done += 1
                if done % PROGRESS_EVERY == 0:
                    print(f"   … обработано {fmt_num(done)} из {fmt_num(len(targets))}", flush=True)

    tasks = [asyncio.create_task(one(u)) for u in targets]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        raise

    stop = " (остановлено вручную)" if cancelled() else ""
    print(f"✔ Готово за {fmt_duration(time.monotonic() - started)}{stop}: "
          f"сохранено {fmt_num(ok)} из {fmt_num(len(targets))}. Папка: {folder}", flush=True)
    if failed:
        shown = ", ".join(failed[:10]) + (f" … и ещё {len(failed) - 10}" if len(failed) > 10 else "")
        print(f"⚠ Не удалось выгрузить ({len(failed)}): {shown}", flush=True)