"""Слеш-команды: /stats, /cancel, /exclude add|remove|list и доставка отчёта в Discord."""

import asyncio
import io
import zipfile
from contextlib import suppress
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands

from config import (
    ALLOWED_USER_ID, DETAIL_LABELS, FORMAT_LABELS, PERIOD_PRESETS, REACTION_LABELS, SORT_LABELS,
    STATS_DIR, UPLOAD_LIMIT, build_period, collection_dir, fmt_num, log, resolve_period,
    save_local_copy, normalize_period_key,
)
from reports import build_report, build_xlsx
from scanner import (
    ScanParams, ScanState, background_tasks, guild_start_date, load_store, looks_like_id,
    norm_name, params_for_guild, running, save_store, scan_guild, sync_only,
)

def is_allowed(interaction: discord.Interaction) -> bool:
    if ALLOWED_USER_ID:
        return interaction.user.id == ALLOWED_USER_ID
    return bool(
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )

# ---------- Доставка ----------

async def send_with_fallback(user, channel, content: str,
                             files: Optional[list[tuple[str, bytes]]] = None) -> Optional[str]:
    """Сначала пробует ЛС, затем канал, где вызвана команда. Возвращает 'dm', 'channel' или None."""
    files = list(files or [])

    def mk(item):
        return discord.File(io.BytesIO(item[1]), filename=item[0])

    for kind, dest, prefix in (("dm", user, ""), ("channel", channel, f"{user.mention} ")):
        if dest is None:
            continue
        try:
            kwargs = {"file": mk(files[0])} if files else {}
            await dest.send(prefix + content, **kwargs)
        except discord.Forbidden:
            continue
        except discord.HTTPException:
            log.exception("Не удалось отправить (%s)", kind)
            continue
        for item in files[1:]:
            try:
                await dest.send(file=mk(item))
            except discord.HTTPException:
                log.exception("Не удалось отправить файл %s (%s)", item[0], kind)
        return kind
    return None

async def deliver(guild, requester, channel, params: ScanParams, state: ScanState, result):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"stats_{guild.id}_{stamp}"
    folder = collection_dir(params.start_date, params.end_date, STATS_DIR,
                            full_history=params.full_history)
    save_locally = bool(STATS_DIR) or params.full_history

    files: list[tuple[str, bytes]] = []
    if params.fmt in ("xlsx", "both"):
        data = await asyncio.to_thread(build_xlsx, guild.name, params, state, result)
        files.append((base + ".xlsx", data))
    if params.fmt in ("txt", "both"):
        text = await asyncio.to_thread(build_report, guild.name, params, state, result)
        files.append((base + ".txt", text.encode("utf-8")))

    if save_locally:
        for name, data in files:
            await asyncio.to_thread(save_local_copy, name, data, folder)

    to_send: list[tuple[str, bytes]] = []
    too_big: list[str] = []
    notes = ""
    for name, data in files:
        if len(data) > UPLOAD_LIMIT and name.endswith(".txt"):
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr(name, data)
            zipped = zbuf.getvalue()
            if len(zipped) <= UPLOAD_LIMIT:
                to_send.append((name[:-4] + ".zip", zipped))
                notes += " (txt большой, поэтому он в zip)"
                continue
        if len(data) > UPLOAD_LIMIT:
            too_big.append(await asyncio.to_thread(save_local_copy, name, data, folder))
        else:
            to_send.append((name, data))

    if too_big:
        await send_with_fallback(
            requester, channel,
            "⚠️ Часть отчёта слишком велика для отправки в Discord. "
            "Файлы сохранены на компьютере, где запущен бот:\n" + "\n".join(f"`{p}`" for p in too_big),
        )
        if not to_send:
            return

    if state.cancelled:
        status = "⛔ Сбор остановлен, отчёт неполный"
    elif state.failed or state.skipped:
        status = "⚠️ Сбор завершён с пропусками; отчёт может быть неполным"
    else:
        status = "✅ Сбор завершён"
    content = (
        f"{status} за {state.elapsed()}. Просмотрено сообщений: {fmt_num(state.seen)}, "
        f"учтено: {fmt_num(state.counted)}.{notes}"
    )
    if save_locally or too_big:
        content += f"\n📁 Сохранено в: `{folder}`"
    sent = await send_with_fallback(requester, channel, content, to_send)
    if sent is None:
        saved = [await asyncio.to_thread(save_local_copy, n, d, folder) for n, d in to_send]
        log.error("Не удалось отправить отчёт. Он сохранён: %s", ", ".join(saved))

async def run_scan(guild, requester, channel, params: ScanParams, state: ScanState,
                   status_msg: Optional[discord.Message], bot_user_id: int):
    final_text = "✅ Сбор завершён."
    try:
        result = await scan_guild(guild, params, bot_user_id, state, status_msg)
        await deliver(guild, requester, channel, params, state, result)
        if state.cancelled:
            final_text = "⛔ Сбор остановлен вручную."
    except Exception:
        log.exception("Сбор статистики завершился ошибкой")
        final_text = "❌ Сбор прервался из-за ошибки (подробности в консоли бота)."
        await send_with_fallback(requester, channel, final_text)
    finally:
        state.done = True
        running.pop(guild.id, None)
        if status_msg is not None:
            with suppress(discord.HTTPException):
                await status_msg.edit(content=f"{final_text} (заняло {state.elapsed()})")

# ---------- /stats и /cancel ----------

@app_commands.command(
    name="stats",
    description="Собрать статистику по истории сервера и прислать файл (Excel/txt)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    user="Считать только этого пользователя (опционально)",
    channel="Только этот канал и его ветки (опционально)",
    period="Готовый период: prev-month, last30d и т. д. (учитываются только завершённые сутки)",
    start_date="Дата начала, дд.мм.гггг (вместо period)",
    end_date="Дата конца включительно, дд.мм.гггг (по умолчанию — вчера; вместо period)",
    compare="Сравнить с предыдущим периодом такой же длины (читает вдвое больше)",
    sort="Порядок пользователей в отчёте (по умолчанию — по количеству)",
    detail="Детализация матрицы: дни/недели/месяцы (по умолчанию — авто)",
    fmt="Формат файла (по умолчанию — Excel)",
    exclude="Доп. чаты для пропуска: точные названия через запятую (опционально)",
    reactions="Реакции: счётчики (по умолчанию), «полный» — кто ставил (долго), или не собирать",
    live="Читать напрямую из Discord, не используя базу данных (медленнее)",
    nosync="Не читать Discord: построить отчёт только из базы (быстро, по уже собранным данным)",
)
@app_commands.rename(fmt="format")
@app_commands.choices(
    sort=[app_commands.Choice(name=label, value=key) for key, label in SORT_LABELS.items()],
    detail=[app_commands.Choice(name=label, value=key) for key, label in DETAIL_LABELS.items()],
    fmt=[app_commands.Choice(name=label, value=key) for key, label in FORMAT_LABELS.items()],
    reactions=[app_commands.Choice(name=label, value=key) for key, label in REACTION_LABELS.items()],
)
async def stats(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    channel: Optional[discord.TextChannel] = None,
    period: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    compare: Optional[bool] = None,
    sort: Optional[app_commands.Choice[str]] = None,
    detail: Optional[app_commands.Choice[str]] = None,
    fmt: Optional[app_commands.Choice[str]] = None,
    exclude: Optional[str] = None,
    reactions: Optional[app_commands.Choice[str]] = None,
    live: Optional[bool] = None,
    nosync: Optional[bool] = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if not is_allowed(interaction):
        await interaction.followup.send("Нет доступа к этой команде.", ephemeral=True)
        return
    if live and nosync:
        await interaction.followup.send(
            "Параметры live и nosync несовместимы: live вообще не использует базу.", ephemeral=True)
        return

    guild = interaction.guild
    if guild.id in running:
        await interaction.followup.send(
            "Сбор на этом сервере уже идёт. Дождитесь результата или остановите его через /cancel.",
            ephemeral=True,
        )
        return

    try:
        d_start, d_end, p_key, p_label = build_period(period, start_date, end_date, bool(compare))
    except ValueError as e:
        await interaction.followup.send(str(e), ephemeral=True)
        return

    params = ScanParams(
        start_date=d_start, end_date=d_end, user=user, channel=channel,
        sort_key=sort.value if sort else "count",
        detail=detail.value if detail else "auto",
        fmt=fmt.value if fmt else "xlsx",
        exclude=exclude or "",
        period_key=p_key, period_label=p_label, compare=bool(compare),
        reactions=reactions.value if reactions else "counts",
        sync=not nosync, live=bool(live),
    )

    state = ScanState(title=guild.name)
    params = params_for_guild(guild, params, state)
    if params is None:
        await interaction.followup.send(
            "В выбранном периоде у этого сервера нет истории: она начинается с "
            f"{guild_start_date(guild).strftime('%d.%m.%Y')}.",
            ephemeral=True,
        )
        return
    running[guild.id] = state
    try:
        status_msg = None
        dm_ok = True
        try:
            status_msg = await interaction.user.send("⏳ Начинаю сбор статистики…")
        except discord.HTTPException:
            dm_ok = False

        where = (
            "Прогресс и результат пришлю вам в личные сообщения."
            if dm_ok else
            "Личные сообщения закрыты, поэтому результат придёт в этот канал "
            "(прогресс — только в консоли бота)."
        )
        slow = ("\n⚠ Для реакций выбран режим «полный»: сбор может идти заметно дольше обычного."
                if params.reactions == "full" else "")
        await interaction.followup.send(
            f"Сбор запущен. Это может занять много времени. {where}{slow}\n"
            "Остановить: /cancel.",
            ephemeral=True,
        )

        task = asyncio.create_task(run_scan(
            guild, interaction.user, interaction.channel, params, state,
            status_msg, interaction.client.user.id,
        ))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    except Exception:
        running.pop(guild.id, None)
        raise

@stats.autocomplete("period")
async def stats_period_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.strip().lower()
    out = [
        app_commands.Choice(name=f"{key} — {label}"[:100], value=key)
        for key, label in PERIOD_PRESETS.items()
        if not cur or cur in key or cur in label.lower()
    ]
    key = normalize_period_key(current)
    if key and key not in PERIOD_PRESETS:
        with suppress(ValueError):          # свободный ввод вроде last45d
            _s, _e, k, label = resolve_period(key)
            out.insert(0, app_commands.Choice(name=f"{k} — {label}"[:100], value=k))
    return out[:25]

@app_commands.command(name="cancel", description="Остановить текущий сбор (отчёт будет неполным)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def cancel(interaction: discord.Interaction):
    if not is_allowed(interaction):
        await interaction.response.send_message("Нет доступа к этой команде.", ephemeral=True)
        return
    state = running.get(interaction.guild.id)
    if state is None:
        await interaction.response.send_message("Сейчас сбор не запущен.", ephemeral=True)
        return
    state.cancelled = True
    await interaction.response.send_message(
        "Останавливаю. Пришлю то, что успел собрать.", ephemeral=True
    )

# ---------- /sync ----------

async def run_sync(guild, requester, channel, reactions: str, state: ScanState,
                   status_msg: Optional[discord.Message], bot_user_id: int):
    final_text = "✅ База обновлена."
    try:
        await sync_only(guild, reactions, bot_user_id, state, status_msg)
        if state.cancelled:
            final_text = "⛔ Обновление остановлено вручную (то, что успело записаться, сохранено)."
        elif state.failed or state.skipped:
            final_text = "⚠️ База обновлена с пропусками (подробности ниже)."
        lines = [
            f"{final_text} Заняло {state.elapsed()}.",
            f"Просмотрено сообщений: {fmt_num(state.seen)}, обработано новых/обновлённых: {fmt_num(state.counted)}.",
            f"Каналов проверено: {state.channels_done}/{state.total_channels}"
            f" (без изменений пропущено: {state.inactive}).",
        ]
        if state.skipped:
            lines.append(f"Нет доступа: {len(state.skipped)} канал(ов).")
        if state.failed:
            lines.append(f"Ошибки чтения: {len(state.failed)}.")
        lines.extend(f"• {n}" for n in state.notes[:5])
        await send_with_fallback(requester, channel, "\n".join(lines)[:1900])
    except Exception:
        log.exception("Обновление базы завершилось ошибкой")
        final_text = "❌ Обновление базы прервалось из-за ошибки (подробности в консоли бота)."
        await send_with_fallback(requester, channel, final_text)
    finally:
        state.done = True
        running.pop(guild.id, None)
        if status_msg is not None:
            with suppress(discord.HTTPException):
                await status_msg.edit(content=f"{final_text} (заняло {state.elapsed()})")

@app_commands.command(
    name="sync",
    description="Обновить базу данных из Discord (только новое), без построения отчётов",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    reactions="Реакции: счётчики (перечитывает последние 14 дней), «полный» (медленно) или не трогать (быстрее всего)",
)
@app_commands.choices(
    reactions=[app_commands.Choice(name=label, value=key) for key, label in REACTION_LABELS.items()],
)
async def sync_cmd(
    interaction: discord.Interaction,
    reactions: Optional[app_commands.Choice[str]] = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if not is_allowed(interaction):
        await interaction.followup.send("Нет доступа к этой команде.", ephemeral=True)
        return

    guild = interaction.guild
    if guild.id in running:
        await interaction.followup.send(
            "На этом сервере уже идёт сбор. Дождитесь результата или остановите его через /cancel.",
            ephemeral=True,
        )
        return

    mode = reactions.value if reactions else "counts"
    state = ScanState(title=guild.name)
    running[guild.id] = state
    try:
        status_msg = None
        dm_ok = True
        try:
            status_msg = await interaction.user.send("⏳ Начинаю обновление базы…")
        except discord.HTTPException:
            dm_ok = False

        where = ("Прогресс и результат пришлю вам в личные сообщения." if dm_ok else
                 "Личные сообщения закрыты, поэтому результат придёт в этот канал.")
        slow = ("\n⚠ Для реакций выбран режим «полный»: обновление может идти долго."
                if mode == "full" else "")
        await interaction.followup.send(
            f"Обновление базы запущено. {where}{slow}\nОстановить: /cancel.", ephemeral=True)

        task = asyncio.create_task(run_sync(
            guild, interaction.user, interaction.channel, mode, state,
            status_msg, interaction.client.user.id,
        ))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    except Exception:
        running.pop(guild.id, None)
        raise

# ---------- /exclude ----------

exclude_group = app_commands.Group(
    name="exclude",
    description="Чаты, которые пропускаются при сборе статистики",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)

def _stored_label(entry: dict) -> str:
    name = entry.get("name") or ""
    if entry.get("id"):
        return f"{name} (ID {entry['id']})" if name else f"ID {entry['id']}"
    return name

@exclude_group.command(name="add", description="Добавить чат в постоянный список исключений")
@app_commands.describe(name="Точное название канала/ветки или его ID (подсказки — при вводе)")
async def exclude_add(interaction: discord.Interaction, name: str):
    if not is_allowed(interaction):
        await interaction.response.send_message("Нет доступа к этой команде.", ephemeral=True)
        return
    guild = interaction.guild
    token = name.strip()
    if not token:
        await interaction.response.send_message("Пустое название.", ephemeral=True)
        return

    extra = ""
    if looks_like_id(token):
        ch = guild.get_channel_or_thread(int(token))
        entry = {"name": ch.name if ch else "", "id": int(token)}
        if ch is None:
            extra = " Канал с таким ID сейчас не найден, но правило сохранено."
    else:
        target = norm_name(token)
        matches = [c for c in guild.channels
                   if not isinstance(c, discord.CategoryChannel) and norm_name(c.name) == target]
        if len(matches) == 1:
            entry = {"name": matches[0].name, "id": matches[0].id}
        else:
            entry = {"name": token.strip().lstrip("#").strip(), "id": None}
            if not matches:
                extra = (" Канала с таким точным названием сейчас нет (возможно, это ветка или опечатка) — "
                         "правило всё равно сохранено.")
            else:
                extra = f" Каналов с таким названием: {len(matches)} — будут пропущены все."

    store = load_store()
    items = store.setdefault(str(guild.id), [])
    for e in items:
        same_id = entry["id"] and e.get("id") == entry["id"]
        same_name = (not entry["id"] and not e.get("id")
                     and norm_name(e.get("name", "")) == norm_name(entry["name"]))
        if same_id or same_name:
            await interaction.response.send_message(f"Уже в списке: {_stored_label(e)}", ephemeral=True)
            return
    items.append(entry)
    save_store(store)
    await interaction.response.send_message(
        f"Добавлено в исключения: {_stored_label(entry)}.{extra}", ephemeral=True
    )

@exclude_add.autocomplete("name")
async def exclude_add_autocomplete(interaction: discord.Interaction, current: str):
    if not is_allowed(interaction) or interaction.guild is None:
        return []
    cur = norm_name(current)
    out: list[app_commands.Choice[str]] = []
    for c in interaction.guild.channels:
        if isinstance(c, discord.CategoryChannel):
            continue
        if cur in norm_name(c.name):
            out.append(app_commands.Choice(name=c.name[:100] or "?", value=c.name[:100]))
            if len(out) >= 25:
                break
    return out

@exclude_group.command(name="remove", description="Убрать чат из постоянного списка исключений")
@app_commands.describe(name="Название или ID из списка (/exclude list)")
async def exclude_remove(interaction: discord.Interaction, name: str):
    if not is_allowed(interaction):
        await interaction.response.send_message("Нет доступа к этой команде.", ephemeral=True)
        return
    guild = interaction.guild
    token = name.strip()
    store = load_store()
    keep, removed = [], []
    for e in store.get(str(guild.id), []):
        if norm_name(e.get("name", "")) == norm_name(token) or str(e.get("id") or "") == token:
            removed.append(e)
        else:
            keep.append(e)
    if not removed:
        await interaction.response.send_message(
            "В постоянном списке такого нет. (Чаты из IGNORED_CHATS в коде командой не убираются.)",
            ephemeral=True,
        )
        return
    store[str(guild.id)] = keep
    save_store(store)
    await interaction.response.send_message(
        "Убрано из исключений: " + ", ".join(_stored_label(e) for e in removed), ephemeral=True
    )

@exclude_remove.autocomplete("name")
async def exclude_remove_autocomplete(interaction: discord.Interaction, current: str):
    if not is_allowed(interaction) or interaction.guild is None:
        return []
    cur = norm_name(current)
    out = []
    for e in load_store().get(str(interaction.guild.id), []):
        label = e.get("name") or str(e.get("id"))
        if cur in norm_name(label):
            out.append(app_commands.Choice(name=_stored_label(e)[:100], value=label[:100]))
            if len(out) >= 25:
                break
    return out

@exclude_group.command(name="list", description="Показать, какие чаты сейчас пропускаются")
async def exclude_list(interaction: discord.Interaction):
    from config import IGNORED_CHATS
    if not is_allowed(interaction):
        await interaction.response.send_message("Нет доступа к этой команде.", ephemeral=True)
        return
    code_items = sorted(str(x) for x in IGNORED_CHATS.get(interaction.guild.id, ()))
    stored = [_stored_label(e) for e in load_store().get(str(interaction.guild.id), [])]
    text = (
        "**Пропускаются всегда (в коде, IGNORED_CHATS этого сервера):** "
        + (", ".join(code_items) if code_items else "—")
        + "\n**Постоянный список этого сервера:** "
        + (", ".join(stored) if stored else "—")
        + "\nСовпадение точное (регистр не важен). Исключённый канал пропускается вместе с ветками."
    )
    await interaction.response.send_message(text[:1900], ephemeral=True)

# ---------- Регистрация ----------

async def on_app_command_error(interaction: discord.Interaction,
                               error: app_commands.AppCommandError):
    log.exception("Ошибка команды", exc_info=error)
    msg = "Произошла внутренняя ошибка (подробности в консоли бота)."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

def register_commands(bot: discord.Client):
    """Подключает все слеш-команды к дереву команд бота (bot.tree)."""
    bot.tree.add_command(stats)
    bot.tree.add_command(cancel)
    bot.tree.add_command(sync_cmd)
    bot.tree.add_command(exclude_group)
    bot.tree.error(on_app_command_error)