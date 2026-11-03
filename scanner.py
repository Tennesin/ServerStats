"""Сбор данных из Discord: поиск каналов, правила исключений, синхронизация с базой и чтение истории."""

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Optional

import discord

from config import (
    ADMIN_ROLES, AUDIT_ENABLED, AUDIT_MAX_ENTRIES, AUDIT_RETENTION_DAYS, AUDIT_SKIP_ACTIONS,
    CONCURRENCY, DATE_FMT, EXCLUDE_FILE, GUILD_START_DATES, IGNORED_CHATS, IGNORE_USERS,
    MERGE_USERS, MIN_START_DATE, PROGRESS_INTERVAL, REACTION_MAX_REQUESTS, REACTION_MAX_USERS,
    REACTION_REFRESH_DAYS, SPAM_FILTER, SYNC_BATCH, TZ,
    fmt_duration, fmt_list, fmt_num, log, parse_date, today_local,
)
from db import get_db, set_meta
from emoji_utils import normalize_unicode, parse_emoji

# Считаем только обычные сообщения и ответы; системные и сообщения ботов не учитываются.
COUNTED_TYPES = {discord.MessageType.default, discord.MessageType.reply}

running: dict[int, "ScanState"] = {}      # guild_id -> активный сбор
background_tasks: set[asyncio.Task] = set()

def chan_label(ch) -> str:
    if isinstance(ch, discord.Thread) and ch.parent is not None:
        return f"#{ch.parent.name} › {ch.name}"
    return f"#{ch.name}"

# ---------- Параметры, состояние, результат ----------

@dataclass
class ScanParams:
    start_date: Optional[date]
    end_date: Optional[date]
    user: Optional[discord.Member]
    channel: Optional[discord.TextChannel]
    sort_key: str
    detail: str = "auto"
    fmt: str = "xlsx"
    exclude: str = ""
    period_key: str = ""       # ключ пресета ("prev-month"), если задан
    period_label: str = ""     # человекочитаемое название пресета
    compare: bool = False      # сравнить с предыдущим периодом той же длины
    reactions: str = "counts"  # реакции: "off" / "counts" (счётчики) / "full" (с авторами реакций)
    sync: bool = True          # False — не обращаться к Discord за сообщениями, собрать отчёт только из базы
    live: bool = False         # True — читать Discord напрямую, без базы данных (как раньше)

    @property
    def prev_range(self) -> Optional[tuple[date, date]]:
        """Предыдущий период той же длины, вплотную перед текущим."""
        if not (self.compare and self.start_date and self.end_date):
            return None
        length = (self.end_date - self.start_date).days + 1
        prev_end = self.start_date - timedelta(days=1)
        return prev_end - timedelta(days=length - 1), prev_end

    @property
    def full_history(self) -> bool:
        """True, если отчёт запрошен за всю историю (без пресета и без дат)."""
        return self.period_key == "all"

@dataclass
class StaffMember:
    name: str                  # отображаемый ник
    username: str              # оригинальный логин
    kind: str                  # "owner" или "admin"
    roles: list = field(default_factory=list)   # какие роли из ADMIN_ROLES у человека

@dataclass
class StaffInfo:
    owner_id: Optional[int] = None
    members: dict = field(default_factory=dict)          # uid -> StaffMember
    unmatched_roles: list = field(default_factory=list)  # правила из ADMIN_ROLES без совпадений
    partial: bool = False                                # список участников мог загрузиться не полностью

@dataclass
class ScanResult:
    user_day: dict = field(default_factory=lambda: defaultdict(int))      # (uid, date) -> n
    user_channel: dict = field(default_factory=lambda: defaultdict(int))  # (uid, channel_id) -> n
    heat: dict = field(default_factory=lambda: defaultdict(int))          # (weekday, hour) -> n
    user_heat: dict = field(default_factory=lambda: defaultdict(int))     # (uid, weekday, hour) -> n
    replies: dict = field(default_factory=lambda: defaultdict(int))       # uid -> n
    reply_pairs: dict = field(default_factory=lambda: defaultdict(int))   # (кто ответил, кому) -> n
    mention_pairs: dict = field(default_factory=lambda: defaultdict(int)) # (кто тегнул, кого) -> n
    emoji_user: dict = field(default_factory=lambda: defaultdict(int))      # (uid, ключ эмодзи, только_эмодзи 0/1) -> n
    emoji_only_day: dict = field(default_factory=lambda: defaultdict(int))  # (uid, date) -> сообщений «только эмодзи»
    emoji_info: dict = field(default_factory=dict)                          # ключ -> (имя, анимированный 0/1, внешний 0/1)
    # Реакции под сообщениями (снимок на момент сбора)
    reaction_mode: str = "off"                                            # "off" / "counts" / "full"
    reaction_recv: dict = field(default_factory=lambda: defaultdict(int))   # (uid автора сообщения, ключ эмодзи) -> n
    reaction_msgs: dict = field(default_factory=lambda: defaultdict(int))   # uid автора -> сообщений с реакциями
    reaction_given: dict = field(default_factory=lambda: defaultdict(int))  # (кто поставил, ключ) -> n (только full)
    reaction_pairs: dict = field(default_factory=lambda: defaultdict(int))  # (кто поставил, кому) -> n (только full)
    reaction_self_or_bot: int = 0                                         # исключено: реакции ботов и самореакции (full)
    reaction_unattributed: int = 0                                        # реакции без известного автора (full)
    reaction_capped: int = 0                                              # 1, если упёрлись в лимит запросов
    reaction_errors: int = 0                                              # ошибок чтения списка людей
    reply_unresolved: int = 0                                             # ответов, где адресат неизвестен
    reply_self_or_bot: int = 0                                            # ответы себе и ботам (не учитываются)
    # Журнал аудита (действия администрации)
    audit_mode: str = "off"                                               # "off" / "ok" / "denied" / "failed" / "partial" (в сумме)
    audit_actions: dict = field(default_factory=lambda: defaultdict(int))      # код действия -> записей
    audit_units: dict = field(default_factory=lambda: defaultdict(int))        # код действия -> затронуто объектов
    audit_user_action: dict = field(default_factory=lambda: defaultdict(int))  # (uid автора, код) -> записей
    audit_user_units: dict = field(default_factory=lambda: defaultdict(int))   # (uid автора, код) -> объектов
    audit_day: dict = field(default_factory=lambda: defaultdict(int))          # date -> записей
    audit_names: dict = field(default_factory=dict)                            # uid автора -> ник
    audit_total: int = 0                                                  # учтено записей
    audit_skipped: int = 0                                                # пропущено по AUDIT_SKIP_ACTIONS
    audit_unknown_actor: int = 0                                          # записей без известного автора
    audit_truncated: int = 0                                              # 1, если упёрлись в AUDIT_MAX_ENTRIES
    audit_first: Optional[date] = None                                    # дата первой учтённой записи
    audit_last: Optional[date] = None                                     # дата последней учтённой записи
    names: dict = field(default_factory=dict)                             # uid -> ник
    usernames: dict = field(default_factory=dict)                         # uid -> оригинальный ник
    avatars: dict = field(default_factory=dict)                           # uid -> URL аватарки
    merged: bool = False                                                  # True у суммарного результата
    channel_names: dict = field(default_factory=dict)                     # channel_id -> название
    user_server: dict = field(default_factory=lambda: defaultdict(int))   # (uid, сервер) -> n (только в сумме)
    prev: Optional["ScanResult"] = None                                   # предыдущий период
    staff: Optional["StaffInfo"] = None                                   # администрация сервера
    staff_servers: dict = field(default_factory=dict)                     # сервер -> StaffInfo (только в сумме)

    @property
    def total(self) -> int:
        return sum(self.user_day.values())

class ScanState:
    def __init__(self, title: str = ""):
        self.title = title
        self.started = time.monotonic()
        self.cancelled = False
        self.done = False
        self.total_channels = 0
        self.channels_done = 0
        self.in_progress: dict[int, str] = {}
        self.seen = 0
        self.counted = 0
        self.inactive = 0
        self.text_seen = 0
        self.skipped: list[str] = []
        self.failed: list[str] = []
        self.excluded: list[str] = []
        self.unused_rules: list[str] = []
        self.notes: list[str] = []
        self.reaction_msgs = 0        # сообщений, под которыми есть реакции
        self.reaction_requests = 0    # запросов «кто поставил» (страниц по 100 человек)
        self.reaction_errors = 0
        self.reaction_capped = False
        self.spam_filtered = 0        # эмодзи и реакций, отброшенных антиспам-фильтрами

    def elapsed(self) -> str:
        return fmt_duration(time.monotonic() - self.started)

    def progress_text(self) -> str:
        head = f"[{self.title}] " if self.title else ""
        if self.total_channels == 0:
            return f"⏳ {head}Ищу каналы и ветки… (прошло: {self.elapsed()})"
        now = list(self.in_progress.values())
        reading = fmt_list(now, 4) if now else "—"
        rx = ""
        if self.reaction_msgs:
            rx = f"\nСообщений с реакциями: {fmt_num(self.reaction_msgs)}"
            if self.reaction_requests:
                rx += f" • запросов «кто поставил»: {fmt_num(self.reaction_requests)}"
        return (
            f"⏳ {head}Идёт сбор. Каналов обработано: {self.channels_done}/{self.total_channels}\n"
            f"Сейчас читаю: {reading}\n"
            f"Просмотрено сообщений: {fmt_num(self.seen)} • учтено: {fmt_num(self.counted)} "
            f"• прошло: {self.elapsed()}{rx}"
        )

def period_bounds(params: ScanParams):
    """Границы чтения. При сравнении начало сдвигается на начало предыдущего периода."""
    first = params.prev_range[0] if params.prev_range else params.start_date
    start_dt = datetime.combine(first, dtime.min, tzinfo=TZ) if first else None
    end_dt = (
        datetime.combine(params.end_date + timedelta(days=1), dtime.min, tzinfo=TZ)
        if params.end_date else None
    )
    return start_dt, end_dt

def guild_start_date(guild: discord.Guild) -> date:
    """Первый день, с которого у сервера есть осмысленная история."""
    override = GUILD_START_DATES.get(guild.id)
    if override:
        return override
    return max(MIN_START_DATE, guild.created_at.astimezone(TZ).date())

def params_for_guild(guild: discord.Guild, params: ScanParams,
                     state: ScanState) -> Optional[ScanParams]:
    """Копия параметров с началом периода, подрезанным под историю сервера.
    None — в выбранном периоде у сервера истории нет (читать нечего).
    Пояснения пишутся в state.notes и попадают в лист «Служебное»."""
    g_start = guild_start_date(guild)
    s, e = params.start_date, params.end_date
    if e and e < g_start:
        return None
    new_s = max(s, g_start) if s else g_start
    if s and new_s != s:
        state.notes.append(
            f"История сервера начинается с {g_start.strftime(DATE_FMT)}: период сокращён "
            f"(запрошено с {s.strftime(DATE_FMT)})."
        )
    compare = params.compare
    if compare and e:
        length = (e - new_s).days + 1
        if new_s - timedelta(days=length) < g_start:
            compare = False
            state.notes.append(
                "Сравнение с предыдущим периодом отключено: он начался бы раньше "
                f"начала истории сервера ({g_start.strftime(DATE_FMT)})."
            )
    return replace(params, start_date=new_s, compare=compare)

# ---------- Правила исключения чатов ----------

def norm_name(text: str) -> str:
    return str(text).strip().lstrip("#").strip().casefold()

def looks_like_id(token: str) -> bool:
    return token.isdigit() and len(token) >= 17

class ExcludeRules:
    """Набор правил: точные названия (без учёта регистра) и ID каналов."""

    def __init__(self):
        self.names: dict[str, str] = {}
        self.ids: dict[int, str] = {}
        self.hit: set[str] = set()

    def add_name(self, text: str):
        n = norm_name(text)
        if n:
            self.names.setdefault(n, str(text).strip())

    def add_id(self, channel_id: int, label: Optional[str] = None):
        self.ids.setdefault(int(channel_id), label or str(channel_id))

    def add_token(self, token):
        if isinstance(token, int):
            self.add_id(token)
            return
        token = str(token).strip()
        if not token:
            return
        if looks_like_id(token):
            self.add_id(int(token))
        else:
            self.add_name(token)

    def matches(self, ch) -> bool:
        if ch.id in self.ids:
            self.hit.add(self.ids[ch.id])
            return True
        n = norm_name(ch.name)
        if n in self.names:
            self.hit.add(self.names[n])
            return True
        return False

    def unused(self) -> list[str]:
        all_labels = set(self.names.values()) | set(self.ids.values())
        return sorted(all_labels - self.hit, key=str.casefold)

class IdentityRules:
    """Игнорируемые аккаунты и слияние аккаунтов для одного сервера."""

    def __init__(self, ignored=frozenset(), aliases=None):
        self.ignored = frozenset(ignored)
        self.aliases = dict(aliases or {})      # ID второго аккаунта -> ID главного

    def skip(self, raw_id: int) -> bool:
        return raw_id in self.ignored

    def canon(self, raw_id: int) -> int:
        return self.aliases.get(raw_id, raw_id)

NO_IDENTITY = IdentityRules()

def build_identity(guild_id: int) -> IdentityRules:
    ignored: set[int] = set()
    for item in IGNORE_USERS:
        try:
            if isinstance(item, (tuple, list)):
                uid = int(item[0])
                gid = int(item[1]) if len(item) > 1 and item[1] is not None else None
            else:
                uid, gid = int(item), None
        except (TypeError, ValueError, IndexError):
            log.warning("IGNORE_USERS: запись %r пропущена (неверный формат)", item)
            continue
        if gid is None or gid == guild_id:
            ignored.add(uid)

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for item in MERGE_USERS:
        try:
            a, b = int(item[0]), int(item[1])
            gid = int(item[2]) if len(item) > 2 and item[2] is not None else None
        except (TypeError, ValueError, IndexError):
            log.warning("MERGE_USERS: запись %r пропущена (неверный формат)", item)
            continue
        if gid is not None and gid != guild_id:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra          # главным остаётся первый указанный

    return IdentityRules(ignored, {x: find(x) for x in parent})

def load_store() -> dict:
    try:
        with open(EXCLUDE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("Не удалось прочитать %s", EXCLUDE_FILE)
        return {}

def save_store(data: dict):
    tmp = EXCLUDE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EXCLUDE_FILE)

def build_rules(guild_id: int, extra_text: Optional[str]) -> ExcludeRules:
    rules = ExcludeRules()
    for item in IGNORED_CHATS.get(guild_id, ()):
        rules.add_token(item)
    for entry in load_store().get(str(guild_id), []):
        if entry.get("id"):
            rules.add_id(entry["id"], entry.get("name") or str(entry["id"]))
        elif entry.get("name"):
            rules.add_name(entry["name"])
    if extra_text:
        for tok in re.split(r"[,;\n]", extra_text):
            rules.add_token(tok)
    return rules

# ---------- Поиск каналов ----------

async def collect_channels(guild: discord.Guild, params: ScanParams,
                           rules: ExcludeRules, state: ScanState, bot_user_id: int):
    """Каналы/ветки для чтения. Исключённые, недоступные и заведомо пустые
    за период в список не попадают (учитываются в state)."""
    me = guild.me or await guild.fetch_member(bot_user_id)
    only = params.channel
    start_dt, end_dt = period_bounds(params)

    excluded_ids: set[int] = set()
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        if rules.matches(ch):
            excluded_ids.add(ch.id)
            state.excluded.append(chan_label(ch))
    if only and only.id in excluded_ids:
        excluded_ids.discard(only.id)
        with suppress(ValueError):
            state.excluded.remove(chan_label(only))
        state.notes.append(
            f"Канал {chan_label(only)} указан в фильтре явно, поэтому правило исключения для него не применялось."
        )

    def thread_excluded(t: discord.Thread) -> bool:
        if t.parent_id in excluded_ids:
            return True
        if rules.matches(t):
            state.excluded.append(chan_label(t))
            return True
        return False

    found: dict[int, discord.abc.Messageable] = {}

    for ch in (*guild.text_channels, *guild.voice_channels, *guild.stage_channels):
        if ch.id not in excluded_ids:
            found[ch.id] = ch

    try:
        for t in await guild.active_threads():
            if not thread_excluded(t):
                found[t.id] = t
    except discord.HTTPException as e:
        log.warning("Не удалось получить активные ветки: %s", e)
        state.failed.append(f"Обнаружение активных веток (HTTP {e.status})")

    parents = [only] if only else [*guild.text_channels, *guild.forums]
    for p in parents:
        if p.id in excluded_ids:
            continue
        perms = p.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            continue
        try:
            async for t in p.archived_threads(limit=None):
                if start_dt and t.archive_timestamp and t.archive_timestamp < start_dt:
                    continue   # порядок выдачи API не гарантирован — только пропускаем
                if not thread_excluded(t):
                    found[t.id] = t
        except discord.HTTPException as e:
            log.warning("Не удалось получить публичные архивные ветки %s: %s", chan_label(p), e)
            state.failed.append(f"Публичные архивные ветки {chan_label(p)} (HTTP {e.status})")
        if isinstance(p, discord.TextChannel):
            try:
                async for t in p.archived_threads(private=True, limit=None):
                    if start_dt and t.archive_timestamp and t.archive_timestamp < start_dt:
                        continue
                    if not thread_excluded(t):
                        found[t.id] = t
            except discord.HTTPException as e:
                log.warning("Не удалось получить приватные архивные ветки %s: %s", chan_label(p), e)
                state.failed.append(f"Приватные архивные ветки {chan_label(p)} (HTTP {e.status})")

    if only:
        found = {
            cid: c for cid, c in found.items()
            if cid == only.id or getattr(c, "parent_id", None) == only.id
        }

    readable = []
    for ch in found.values():
        perms = ch.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            state.skipped.append(chan_label(ch))
            continue
        if end_dt and discord.utils.snowflake_time(ch.id) >= end_dt:
            state.inactive += 1     # канал создан уже после конца периода
            continue
        readable.append(ch)

    readable.sort(key=lambda c: c.id)
    state.unused_rules = rules.unused()
    return readable

# ---------- Состав администрации ----------

async def collect_staff(guild: discord.Guild, state: ScanState,
                        ident: IdentityRules = NO_IDENTITY) -> StaffInfo:
    """Основатель (владелец сервера) + участники с ролями из ADMIN_ROLES."""
    info = StaffInfo(owner_id=guild.owner_id)

    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            log.warning("[%s] Не удалось загрузить список участников: %s", guild.name, e)
            state.notes.append("Не удалось полностью загрузить список участников — состав администрации может быть неполным.")
            info.partial = True

    wanted: dict[str, str] = {}      # ключ правила -> как показывать в отчёте
    for item in ADMIN_ROLES.get(guild.id, ()):
        if isinstance(item, int) or looks_like_id(str(item)):
            wanted[f"id:{int(item)}"] = str(item)
        elif norm_name(item):
            wanted[f"name:{norm_name(item)}"] = str(item).strip()

    admin_roles: dict[int, discord.Role] = {}
    hit: set[str] = set()
    for role in guild.roles:
        if role.is_default():
            continue
        for key in (f"id:{role.id}", f"name:{norm_name(role.name)}"):
            if key in wanted:
                admin_roles[role.id] = role
                hit.add(key)
    info.unmatched_roles = [label for key, label in wanted.items() if key not in hit]

    for m in guild.members:
        if m.bot or ident.skip(m.id):
            continue
        roles = [r.name for r in m.roles if r.id in admin_roles]
        if m.id == guild.owner_id:
            kind = "owner"
        elif roles:
            kind = "admin"
        else:
            continue
        key = ident.canon(m.id)
        old = info.members.get(key)
        if old is None:
            info.members[key] = StaffMember(m.display_name, m.name, kind, roles)
        else:                                   # второй аккаунт того же человека
            old.roles = sorted(set(old.roles) | set(roles))
            if kind == "owner":
                old.kind = "owner"

    # Владельца может не быть в кэше — докачиваем отдельно.
    owner_key = ident.canon(guild.owner_id) if guild.owner_id else None
    if guild.owner_id and not ident.skip(guild.owner_id) and owner_key not in info.members:
        try:
            om = await guild.fetch_member(guild.owner_id)
            info.members[owner_key] = StaffMember(
                om.display_name, om.name, "owner",
                [r.name for r in om.roles if r.id in admin_roles])
        except discord.HTTPException:
            info.members[owner_key] = StaffMember(str(guild.owner_id), "", "owner", [])

    if guild.member_count and len(guild.members) < guild.member_count:
        info.partial = True
        state.notes.append(
            f"В кэше {len(guild.members)} из {guild.member_count} участников — "
            "состав администрации мог определиться не полностью."
        )
    if info.unmatched_roles:
        state.notes.append(
            "Роли из ADMIN_ROLES не найдены на сервере: " + ", ".join(info.unmatched_roles)
        )
    return info

# ---------- Антиспам-фильтры эмодзи ----------

def build_spam_rules(guild_id: int) -> dict:
    """SPAM_FILTER сервера -> {ключ эмодзи: [(дата, час_начала, час_конца), ...]}.
    Ключи те же, что у parse_emoji: "c:<id>" и "u:<символ>". Битые правила пропускаются с предупреждением."""
    rules: dict = defaultdict(list)
    for item in SPAM_FILTER.get(guild_id, ()):
        try:
            emo, day_text, h1, h2 = item
            day = parse_date(str(day_text))
            h1, h2 = int(h1), int(h2)
            if day is None or not (0 <= h1 < h2 <= 24):
                raise ValueError("неверная дата или часы")
            emo_s = str(emo).strip()
            if not emo_s:
                raise ValueError("пустой эмодзи")
            key = f"c:{int(emo_s)}" if emo_s.isdigit() else "u:" + normalize_unicode(emo_s)
        except (TypeError, ValueError) as e:
            log.warning("SPAM_FILTER[%s]: правило %r пропущено (%s)", guild_id, item, e)
            continue
        rules[key].append((day, h1, h2))
    return dict(rules)

def is_spam(rules: dict, key: str, local: datetime) -> bool:
    """Попадает ли эмодзи `key` в ручное окно по локальному времени сообщения."""
    for day, h1, h2 in rules.get(key, ()):
        if local.date() == day and h1 <= local.hour < h2:
            return True
    return False

def is_spam_dh(rules: dict, key: str, day: date, hour: int) -> bool:
    """То же, что is_spam, но по готовой локальной дате и часу (для чтения из базы)."""
    for d, h1, h2 in rules.get(key, ()):
        if day == d and h1 <= hour < h2:
            return True
    return False

def filter_emoji(found: list, local: datetime, rules: Optional[dict], state: ScanState) -> list:
    """Оставляет по одному эмодзи каждого типа на сообщение и убирает ручные окна.
    Всё отброшенное идёт в state.spam_filtered."""
    seen: set = set()
    kept = []
    for item in found:
        key = item[0]
        if key in seen:
            state.spam_filtered += 1      # повтор того же эмодзи в этом сообщении
            continue
        seen.add(key)
        if rules and is_spam(rules, key, local):
            state.spam_filtered += 1      # ручной фильтр
            continue
        kept.append(item)
    return kept

# ---------- Реакции (режим --live) ----------

def reaction_key(emoji):
    """(ключ, имя, анимированный, id кастомного). Ключи те же, что у parse_emoji:
    "c:<id>" для кастомных, "u:<символ>" для Unicode (с той же нормализацией)."""
    cid = getattr(emoji, "id", None)
    if cid:
        return (f"c:{cid}", str(getattr(emoji, "name", "") or ""),
                bool(getattr(emoji, "animated", False)), int(cid))
    raw = emoji if isinstance(emoji, str) else (getattr(emoji, "name", None) or str(emoji))
    return "u:" + normalize_unicode(str(raw)), "", False, 0

async def collect_reactions(m: discord.Message, uid: int, target: ScanResult, main: ScanResult,
                            state: ScanState, params: ScanParams, own_emoji_ids,
                            spam_rules: Optional[dict] = None,
                            ident: IdentityRules = NO_IDENTITY):
    """Реакции под одним сообщением (автор uid уже прошёл общие фильтры).
    counts — берём только счётчики; full — читаем, кто поставил, и отсеиваем ботов и самореакции.
    Реакции из ручного антиспам-окна (по времени сообщения) пропускаются."""
    full = params.reactions == "full"
    state.reaction_msgs += 1
    got_any = False
    local = m.created_at.astimezone(TZ)

    for reaction in m.reactions:
        if state.cancelled:
            break
        count = int(reaction.count or 0)
        if count <= 0:
            continue
        key, ename, animated, cid = reaction_key(reaction.emoji)

        if spam_rules and is_spam(spam_rules, key, local):
            state.spam_filtered += count      # вся реакция целиком: и счётчик, и список людей
            continue

        if key not in main.emoji_info:
            external = int(bool(cid) and cid not in own_emoji_ids)
            main.emoji_info[key] = (ename, int(animated), external)

        fetched = 0   # сколько людей реально прочитали (включая отброшенных)
        if full:
            if state.reaction_requests >= REACTION_MAX_REQUESTS:
                if not state.reaction_capped:
                    state.reaction_capped = True
                    state.notes.append(
                        f"Достигнут лимит запросов «кто поставил реакцию» ({REACTION_MAX_REQUESTS}): "
                        "дальше реакции собирались только счётчиками (без списка людей)."
                    )
            else:
                try:
                    async for user in reaction.users(limit=REACTION_MAX_USERS):
                        fetched += 1
                        if state.cancelled:
                            break
                        if ident.skip(user.id):
                            continue
                        giver = ident.canon(user.id)
                        if user.bot or giver == uid:
                            target.reaction_self_or_bot += 1
                            continue
                        target.reaction_recv[(uid, key)] += 1
                        target.reaction_given[(giver, key)] += 1
                        target.reaction_pairs[(giver, uid)] += 1
                        main.names.setdefault(giver, user.display_name)
                        main.usernames.setdefault(giver, user.name)
                        got_any = True
                except discord.HTTPException as e:
                    state.reaction_errors += 1
                    log.warning("Не удалось прочитать авторов реакции под сообщением %s: %s", m.id, e)
                state.reaction_requests += max(1, -(-fetched // 100))

        # Всё, чьих авторов не узнали (счётчики, супер-реакции, лимит, ошибка), идёт как «получено» без фильтра.
        rest = max(0, count - fetched)
        if rest:
            target.reaction_recv[(uid, key)] += rest
            got_any = True
            if full:
                target.reaction_unattributed += rest

    if got_any:
        target.reaction_msgs[uid] += 1

def _reaction_finish(main: ScanResult, state: ScanState, params: ScanParams):
    """Итоговые отметки в результат и пояснения в лист «Служебное»."""
    main.reaction_capped = int(state.reaction_capped)
    main.reaction_errors = state.reaction_errors
    if params.reactions == "off":
        return
    if params.reactions == "counts":
        state.notes.append(
            "Реакции собраны в режиме «счётчики»: в числа входят реакции ботов и самореакции авторов, "
            "кто именно ставил реакции — неизвестно. Реакции — снимок на момент сбора."
        )
        return
    state.notes.append(
        "Реакции собраны в режиме «полный»: реакции ботов и самореакции исключены. "
        "Реакции — снимок на момент сбора."
    )
    unattr = main.reaction_unattributed + (main.prev.reaction_unattributed if main.prev else 0)
    if unattr:
        state.notes.append(
            f"У {fmt_num(unattr)} реакций автор не определён (супер-реакции, лимит или ошибка чтения): "
            "они учтены как «получено», но не очищены от ботов и не входят в «поставлено»."
        )
    if state.reaction_errors:
        state.notes.append(f"Ошибок при чтении авторов реакций: {state.reaction_errors}.")

# ---------- Чтение истории напрямую (режим --live) ----------

async def scan_channels(channels, params: ScanParams, state: ScanState,
                        own_emoji_ids=frozenset(), spam_rules: Optional[dict] = None,
                        ident: IdentityRules = NO_IDENTITY) -> ScanResult:
    start_dt, end_dt = period_bounds(params)
    user_id = ident.canon(params.user.id) if params.user else None

    main = ScanResult()
    main.reaction_mode = params.reactions
    prev: Optional[ScanResult] = None
    if params.prev_range:
        prev = ScanResult()
        prev.reaction_mode = params.reactions
        prev.names = main.names                    # ники и названия каналов общие
        prev.usernames = main.usernames
        prev.channel_names = main.channel_names
        main.prev = prev

    name_ts: dict[int, datetime] = {}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def scan_one(ch):
        async with sem:
            if state.cancelled:
                return
            label = chan_label(ch)
            state.in_progress[ch.id] = label
            try:
                async for m in ch.history(limit=None, after=start_dt, oldest_first=True):
                    if end_dt and m.created_at >= end_dt:
                        break
                    state.seen += 1
                    if state.cancelled:
                        break
                    author = m.author
                    if author.bot or m.type not in COUNTED_TYPES:
                        continue
                    if ident.skip(author.id):          # игнорируемый пользователь
                        continue
                    uid = ident.canon(author.id)       # главный ID при слиянии
                    if user_id and uid != user_id:
                        continue
                    local = m.created_at.astimezone(TZ)
                    day = local.date()
                    target = prev if (prev is not None and day < params.start_date) else main
                    target.user_day[(uid, day)] += 1
                    target.user_channel[(uid, ch.id)] += 1
                    target.heat[(local.weekday(), local.hour)] += 1
                    target.user_heat[(uid, local.weekday(), local.hour)] += 1

                    reply_dst = None
                    if m.type == discord.MessageType.reply:
                        target.replies[uid] += 1
                        ref = m.reference.resolved if m.reference else None
                        if isinstance(ref, discord.Message):
                            dst = ref.author
                            dst_id = ident.canon(dst.id)
                            reply_dst = dst_id
                            if ident.skip(dst.id):
                                pass                    # ответ игнорируемому: не учитываем
                            elif dst.bot or dst_id == uid:
                                target.reply_self_or_bot += 1
                            else:
                                target.reply_pairs[(uid, dst_id)] += 1
                                main.names.setdefault(dst_id, dst.display_name)
                                main.usernames.setdefault(dst_id, dst.name)
                        else:
                            target.reply_unresolved += 1

                    # Теги конкретных людей. Роли, @everyone и @here в m.mentions не попадают.
                    for u in m.mentions:
                        u_id = ident.canon(u.id)
                        if u.bot or ident.skip(u.id) or u_id == uid or u_id == reply_dst:
                            continue
                        target.mention_pairs[(uid, u_id)] += 1
                        main.names.setdefault(u_id, u.display_name)
                        main.usernames.setdefault(u_id, u.name)

                    # Эмодзи (текст доступен только с Message Content Intent)
                    if m.content:
                        state.text_seen += 1
                        found, only = parse_emoji(m.content)
                        if found:
                            # антиспам: один тип эмодзи — один раз на сообщение + ручные окна
                            found = filter_emoji(found, local, spam_rules, state)
                        if found:
                            flag = 1 if only else 0
                            for key, ename, animated, cid in found:
                                target.emoji_user[(uid, key, flag)] += 1
                                if key not in main.emoji_info:
                                    external = int(bool(cid) and cid not in own_emoji_ids)
                                    main.emoji_info[key] = (ename, int(animated), external)
                            if only:
                                target.emoji_only_day[(uid, day)] += 1

                    # Реакции под сообщением (счётчики сразу, авторы — отдельными запросами)
                    if params.reactions != "off" and m.reactions:
                        await collect_reactions(m, uid, target, main, state, params,
                                                own_emoji_ids, spam_rules, ident)

                    main.channel_names.setdefault(ch.id, label)
                    ts = name_ts.get(uid)
                    if ts is None or m.created_at >= ts:
                        name_ts[uid] = m.created_at
                        main.names[uid] = author.display_name
                        main.usernames[uid] = author.name
                        main.avatars[uid] = author.display_avatar.url
                    if target is main:
                        state.counted += 1
            except discord.Forbidden:
                state.skipped.append(label)
            except discord.HTTPException as e:
                log.warning("Ошибка чтения %s: %s", label, e)
                state.failed.append(f"{label} (HTTP {e.status})")
            finally:
                state.in_progress.pop(ch.id, None)
                if not state.cancelled:
                    state.channels_done += 1

    tasks = [asyncio.create_task(scan_one(c)) for c in channels]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    _reaction_finish(main, state, params)
    if state.spam_filtered:
        state.notes.append(f"Количество эмодзи, подвергшихся фильтрации: {fmt_num(state.spam_filtered)}.")
    return main

# ---------- Синхронизация с базой (Discord -> SQLite) ----------

class Batch:
    """Накопитель строк для записи одной пачкой."""

    def __init__(self):
        self.messages, self.mentions, self.emoji = [], [], []
        self.reactions, self.rx_users, self.rx_clear = [], [], []
        self.users_seen, self.users_new, self.emoji_info = [], [], []

    def __len__(self):
        return len(self.messages)

def flush_batch(db, b: Batch, channel_id: int, watermark: int):
    """Одна транзакция: все строки пачки + новый водяной знак канала."""
    with db:
        db.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", b.messages)
        db.executemany("INSERT OR IGNORE INTO mentions VALUES (?,?)", b.mentions)
        db.executemany("INSERT OR IGNORE INTO emoji VALUES (?,?,?)", b.emoji)
        db.executemany(
            "INSERT INTO emoji_info VALUES (?,?,?,?,?) ON CONFLICT(guild_id,key) "
            "DO UPDATE SET name=excluded.name, external=excluded.external", b.emoji_info)
        # люди, известные только по упоминанию/ответу/реакции: существующие записи не затираем
        db.executemany(
            "INSERT OR IGNORE INTO users (guild_id,uid,display_name,username,is_bot) VALUES (?,?,?,?,?)",
            b.users_new)
        # авторы сообщений: обновляем ник и аватар, если сообщение свежее
        db.executemany(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?) ON CONFLICT(guild_id,uid) DO UPDATE SET "
            "display_name=excluded.display_name, username=excluded.username, "
            "avatar_url=excluded.avatar_url, last_seen_ts=excluded.last_seen_ts "
            "WHERE excluded.last_seen_ts >= users.last_seen_ts", b.users_seen)
        # реакции этих сообщений перезаписываются целиком (снимок мог измениться)
        db.executemany("DELETE FROM reactions WHERE message_id=?", [(i,) for i in b.rx_clear])
        db.executemany("DELETE FROM reaction_users WHERE message_id=?", [(i,) for i in b.rx_clear])
        db.executemany("INSERT INTO reactions VALUES (?,?,?,?)", b.reactions)
        db.executemany("INSERT OR IGNORE INTO reaction_users VALUES (?,?,?)", b.rx_users)
        db.execute("UPDATE channels SET last_synced_id = max(last_synced_id, ?) WHERE channel_id = ?",
                   (watermark, channel_id))

async def store_message(b: Batch, m: discord.Message, ch, guild: discord.Guild, params: ScanParams,
                        state: ScanState, own_emoji_ids):
    """Раскладывает одно сообщение по строкам. Игнор, слияние и антиспам здесь НЕ применяются
    (они применяются при чтении из базы)."""
    local = m.created_at.astimezone(TZ)
    ts = int(m.created_at.timestamp())
    author = m.author
    b.users_seen.append((guild.id, author.id, author.display_name, author.name,
                         author.display_avatar.url, 0, ts))

    # Ответ
    reply_to, reply_state = None, 0
    if m.type == discord.MessageType.reply:
        reply_state = 2
        ref = m.reference.resolved if m.reference else None
        if isinstance(ref, discord.Message):
            reply_to, reply_state = ref.author.id, 1
            b.users_new.append((guild.id, ref.author.id, ref.author.display_name,
                                ref.author.name, int(ref.author.bot)))

    # Теги людей
    for u in m.mentions:
        b.mentions.append((m.id, u.id))
        b.users_new.append((guild.id, u.id, u.display_name, u.name, int(u.bot)))

    # Эмодзи в тексте: один тип — один раз на сообщение
    if m.content:
        found, only = parse_emoji(m.content)
        seen_keys = set()
        for key, ename, animated, cid in found:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            b.emoji.append((m.id, key, int(only)))
            external = int(bool(cid) and cid not in own_emoji_ids)
            b.emoji_info.append((guild.id, key, ename, int(animated), external))

    # Реакции
    if params.reactions != "off":
        b.rx_clear.append(m.id)
        full = params.reactions == "full"
        if m.reactions:
            state.reaction_msgs += 1
        for reaction in m.reactions:
            if state.cancelled:
                break
            count = int(reaction.count or 0)
            if count <= 0:
                continue
            key, ename, animated, cid = reaction_key(reaction.emoji)
            external = int(bool(cid) and cid not in own_emoji_ids)
            b.emoji_info.append((guild.id, key, ename, int(animated), external))
            fetched = 0
            if full:
                if state.reaction_requests >= REACTION_MAX_REQUESTS:
                    if not state.reaction_capped:
                        state.reaction_capped = True
                        state.notes.append(
                            f"Достигнут лимит запросов «кто поставил реакцию» ({REACTION_MAX_REQUESTS}): "
                            "дальше реакции собирались только счётчиками (без списка людей).")
                else:
                    try:
                        async for user in reaction.users(limit=REACTION_MAX_USERS):
                            fetched += 1
                            b.rx_users.append((m.id, key, user.id))
                            b.users_new.append((guild.id, user.id, user.display_name,
                                                user.name, int(user.bot)))
                    except discord.HTTPException as e:
                        state.reaction_errors += 1
                        log.warning("Не удалось прочитать авторов реакции под сообщением %s: %s", m.id, e)
                    state.reaction_requests += max(1, -(-fetched // 100))
            b.reactions.append((m.id, key, count, fetched))

    b.messages.append((m.id, guild.id, ch.id, author.id, ts, local.date().isoformat(),
                       local.hour, local.weekday(), int(m.type == discord.MessageType.reply),
                       reply_to, reply_state, int(bool(m.content))))

async def sync_channel(db, guild: discord.Guild, ch, params: ScanParams, state: ScanState, own_emoji_ids):
    label = chan_label(ch)
    wm = db.execute("SELECT last_synced_id FROM channels WHERE channel_id=?", (ch.id,)).fetchone()[0]
    refresh = params.reactions != "off"
    floor_id = discord.utils.time_snowflake(
        datetime.combine(guild_start_date(guild), dtime.min, tzinfo=TZ))
    refresh_id = discord.utils.time_snowflake(datetime.now(TZ) - timedelta(days=REACTION_REFRESH_DAYS))
    # читаем с водяного знака; при включённых реакциях захватываем «свежее окно» для обновления реакций
    if wm and refresh:
        after_id = max(floor_id, min(wm, refresh_id))
    else:
        after_id = max(floor_id, wm)

    # Быстрый пропуск: в канале нет ничего нового и свежего окна тоже
    last = getattr(ch, "last_message_id", None)
    if last and last <= wm and (not refresh or last < refresh_id):
        state.inactive += 1
        state.channels_done += 1
        return

    state.in_progress[ch.id] = label
    b, last_id = Batch(), wm
    try:
        async for m in ch.history(limit=None, after=discord.Object(id=after_id), oldest_first=True):
            if state.cancelled:
                break
            state.seen += 1
            if not (m.author.bot or m.type not in COUNTED_TYPES):
                await store_message(b, m, ch, guild, params, state, own_emoji_ids)
                state.counted += 1
            last_id = max(last_id, m.id)          # после полной обработки сообщения
            if len(b) >= SYNC_BATCH:
                flush_batch(db, b, ch.id, last_id)
                b = Batch()
    except discord.Forbidden:
        state.skipped.append(label)
    except discord.HTTPException as e:
        log.warning("Ошибка чтения %s: %s", label, e)
        state.failed.append(f"{label} (HTTP {e.status})")
    finally:
        flush_batch(db, b, ch.id, last_id)        # то, что успели, сохраняется даже при отмене
        state.in_progress.pop(ch.id, None)
        if not state.cancelled:
            state.channels_done += 1

async def sync_guild(guild: discord.Guild, params: ScanParams, bot_user_id: int,
                     state: ScanState, rules: ExcludeRules):
    """Дочитывает в базу всё новое по серверу."""
    db = get_db()
    started = int(time.time())
    g_start = guild_start_date(guild)

    # С какой даты искать архивные ветки: от прошлой удачной синхронизации (с запасом)
    row = db.execute("SELECT MAX(finished_ts) FROM sync_runs WHERE guild_id=? AND status='ok'",
                     (guild.id,)).fetchone()
    since = g_start
    if row and row[0]:
        since = max(g_start, datetime.fromtimestamp(row[0], TZ).date()
                    - timedelta(days=REACTION_REFRESH_DAYS + 1))

    disc_params = replace(params, start_date=since, end_date=None, channel=None, compare=False)
    channels = await collect_channels(guild, disc_params, rules, state, bot_user_id)
    state.total_channels = len(channels)
    log.info("[%s] синхронизация: каналов к проверке %d; исключено %d; без доступа %d",
             guild.name, len(channels), len(state.excluded), len(state.skipped))

    with db:
        db.executemany(
            "INSERT INTO channels (channel_id,guild_id,parent_id,name,label) VALUES (?,?,?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET name=excluded.name, label=excluded.label, "
            "parent_id=excluded.parent_id",
            [(c.id, guild.id, getattr(c, "parent_id", None), c.name, chan_label(c)) for c in channels])

    try:
        own_emoji_ids = frozenset(e.id for e in await guild.fetch_emojis())
    except discord.HTTPException:
        own_emoji_ids = frozenset(e.id for e in guild.emojis)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(ch):
        async with sem:
            if not state.cancelled:
                await sync_channel(db, guild, ch, params, state, own_emoji_ids)

    tasks = [asyncio.create_task(one(c)) for c in channels]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        raise

    status = "cancelled" if state.cancelled else ("partial" if state.failed else "ok")
    with db:
        db.execute("INSERT INTO sync_runs (guild_id,started_ts,finished_ts,status,reactions_mode,messages_read) "
                   "VALUES (?,?,?,?,?,?)",
                   (guild.id, started, int(time.time()), status, params.reactions, state.seen))

# ---------- Журнал аудита ----------

def _audit_units(entry, code: str) -> int:
    """Сколько объектов затронула запись. Discord склеивает подряд идущие удаления сообщений
    в одну запись с полем count, поэтому для них берём count; для остальных действий — 1."""
    extra = getattr(entry, "extra", None)
    if code in ("message_delete", "message_bulk_delete", "member_disconnect", "member_move"):
        raw = getattr(extra, "count", None)
    elif code == "member_prune":
        raw = getattr(extra, "members_removed", None)
    else:
        raw = None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1

async def collect_audit(guild: discord.Guild, state: ScanState):
    """Дописывает в базу новые записи журнала аудита (с последней сохранённой записи).
    Игнор, слияние и AUDIT_SKIP_ACTIONS применяются позже, при чтении из базы.
    Исключений наружу не бросает: статус пишется в meta, примечания — в state.notes."""
    if not AUDIT_ENABLED:
        return
    db = get_db()
    gid = guild.id
    me = guild.me
    if me is not None and not me.guild_permissions.view_audit_log:
        set_meta(db, f"audit_status:{gid}", "denied")
        state.notes.append("Журнал аудита не собран: у бота нет права «Просмотр журнала аудита».")
        return

    row = db.execute("SELECT MAX(entry_id) FROM audit_entries WHERE guild_id=?", (gid,)).fetchone()
    last_id = row[0] if row and row[0] else 0
    kwargs = {"limit": None, "oldest_first": True}
    if last_id:
        kwargs["after"] = discord.Object(id=last_id)

    rows: list = []
    people: list = []
    stored = 0
    truncated = False
    status = "ok"
    cancelled = False

    def flush():
        nonlocal rows, people
        if rows or people:
            with db:
                db.executemany("INSERT OR IGNORE INTO audit_entries VALUES (?,?,?,?,?,?,?)", rows)
                db.executemany(
                    "INSERT OR IGNORE INTO users (guild_id,uid,display_name,username,is_bot) VALUES (?,?,?,?,?)",
                    people)
        rows, people = [], []

    state.in_progress[-1] = "журнал аудита"
    try:
        async for entry in guild.audit_logs(**kwargs):
            if state.cancelled:
                cancelled = True
                break
            if stored >= AUDIT_MAX_ENTRIES:
                truncated = True
                state.notes.append(
                    f"Журнал аудита обрезан лимитом {fmt_num(AUDIT_MAX_ENTRIES)} записей за один сбор: "
                    "следующий сбор продолжит с этого места.")
                break

            code = getattr(entry.action, "name", None) or str(entry.action)
            user_obj = entry.user
            uid = int(getattr(entry, "user_id", None) or getattr(user_obj, "id", 0) or 0)
            if uid:
                # Боты — по оригинальному названию (name), люди — по нику на сервере.
                if getattr(user_obj, "bot", False):
                    nick = getattr(user_obj, "name", None)
                else:
                    nick = getattr(user_obj, "display_name", None) or getattr(user_obj, "name", None)
                if nick:
                    people.append((gid, uid, str(nick), str(getattr(user_obj, "name", "") or ""),
                                   int(bool(getattr(user_obj, "bot", False)))))
            rows.append((entry.id, gid, int(entry.created_at.timestamp()),
                         entry.created_at.astimezone(TZ).date().isoformat(),
                         code, uid, _audit_units(entry, code)))
            stored += 1
            if len(rows) >= 1000:
                flush()
    except discord.Forbidden:
        status = "denied" if (stored == 0 and not last_id) else "failed"
        state.notes.append("Журнал аудита: Discord отказал в доступе (нужно право «Просмотр журнала аудита»).")
    except discord.HTTPException as e:
        status = "failed"
        log.warning("[%s] Не удалось прочитать журнал аудита: %s", guild.name, e)
        state.notes.append(f"Чтение журнала аудита прервалось ошибкой (HTTP {e.status}): данные аудита неполные.")
    except Exception:
        status = "failed"
        log.exception("[%s] Неожиданная ошибка при чтении журнала аудита", guild.name)
        state.notes.append("Чтение журнала аудита прервалось ошибкой (подробности в консоли бота): данные аудита неполные.")
    finally:
        flush()
        state.in_progress.pop(-1, None)

    if cancelled:
        return                                   # статус не меняем: следующий сбор продолжит с последней записи
    set_meta(db, f"audit_status:{gid}", status)
    set_meta(db, f"audit_truncated:{gid}", "1" if truncated else "0")
    if status == "ok" and not db.execute("SELECT 1 FROM meta WHERE key=?", (f"audit_since:{gid}",)).fetchone():
        set_meta(db, f"audit_since:{gid}", today_local().isoformat())

async def collect_audit_live(guild: discord.Guild, params: ScanParams,
                             state: ScanState, result: ScanResult,
                             ident: IdentityRules = NO_IDENTITY):
    """Прежняя версия (режим --live): читает журнал аудита за период и складывает счётчики в result.
    Никогда не бросает исключений наружу: при проблемах ставит статус в result.audit_mode
    и пишет примечание в state.notes (оно попадёт на лист «Служебное»)."""
    result.audit_mode = "off"
    if not AUDIT_ENABLED:
        return
    if params.channel is not None:
        state.notes.append("Журнал аудита не собирался: задан фильтр по каналу (в журнале нет привязки к каналам).")
        return

    me = guild.me
    if me is not None and not me.guild_permissions.view_audit_log:
        result.audit_mode = "denied"
        state.notes.append("Журнал аудита не собран: у бота нет права «Просмотр журнала аудита».")
        return

    start_dt = (datetime.combine(params.start_date, dtime.min, tzinfo=TZ)
                if params.start_date else None)
    end_dt = (datetime.combine(params.end_date + timedelta(days=1), dtime.min, tzinfo=TZ)
              if params.end_date else None)

    oldest_kept = datetime.now(TZ) - timedelta(days=AUDIT_RETENTION_DAYS)
    if start_dt is None or start_dt < oldest_kept:
        state.notes.append(
            f"Журнал аудита хранится Discord около {AUDIT_RETENTION_DAYS} дней: "
            "записи за начало периода могли быть уже удалены, данные аудита неполные."
        )
    if params.user:
        state.notes.append("Журнал аудита отфильтрован по автору действия (выбран пользователь).")

    kwargs = {"limit": None, "oldest_first": True}
    if start_dt:
        kwargs["after"] = start_dt
    if params.user:
        kwargs["user"] = params.user

    state.in_progress[-1] = "журнал аудита"
    result.audit_mode = "ok"
    try:
        async for entry in guild.audit_logs(**kwargs):
            if state.cancelled:
                break
            if end_dt and entry.created_at >= end_dt:
                break
            if result.audit_total + result.audit_skipped >= AUDIT_MAX_ENTRIES:
                result.audit_truncated = 1
                state.notes.append(
                    f"Журнал аудита обрезан лимитом {fmt_num(AUDIT_MAX_ENTRIES)} записей: данные неполные."
                )
                break

            code = getattr(entry.action, "name", None) or str(entry.action)
            if code in AUDIT_SKIP_ACTIONS:
                result.audit_skipped += 1
                continue

            day = entry.created_at.astimezone(TZ).date()
            user_obj = entry.user
            uid = int(getattr(entry, "user_id", None) or getattr(user_obj, "id", 0) or 0)
            if uid:
                if ident.skip(uid):
                    continue                    # действия игнорируемого не учитываем
                uid = ident.canon(uid)
                # Боты — только по оригинальному названию (name), люди — по нику на сервере.
                if getattr(user_obj, "bot", False):
                    nick = getattr(user_obj, "name", None)
                else:
                    nick = getattr(user_obj, "display_name", None) or getattr(user_obj, "name", None)
                if nick:
                    result.audit_names[uid] = str(nick)
            else:
                result.audit_unknown_actor += 1

            units = _audit_units(entry, code)
            result.audit_total += 1
            result.audit_actions[code] += 1
            result.audit_units[code] += units
            result.audit_user_action[(uid, code)] += 1
            result.audit_user_units[(uid, code)] += units
            result.audit_day[day] += 1
            if result.audit_first is None or day < result.audit_first:
                result.audit_first = day
            if result.audit_last is None or day > result.audit_last:
                result.audit_last = day
    except discord.Forbidden:
        result.audit_mode = "denied" if result.audit_total == 0 else "failed"
        state.notes.append("Журнал аудита: Discord отказал в доступе (нужно право «Просмотр журнала аудита»).")
    except discord.HTTPException as e:
        result.audit_mode = "failed"
        log.warning("[%s] Не удалось прочитать журнал аудита: %s", guild.name, e)
        state.notes.append(f"Чтение журнала аудита прервалось ошибкой (HTTP {e.status}): данные аудита неполные.")
    except Exception:
        result.audit_mode = "failed"
        log.exception("[%s] Неожиданная ошибка при чтении журнала аудита", guild.name)
        state.notes.append("Чтение журнала аудита прервалось ошибкой (подробности в консоли бота): данные аудита неполные.")
    finally:
        state.in_progress.pop(-1, None)

# ---------- Сбор по серверу ----------

async def progress_loop(state: ScanState, status_msg: Optional[discord.Message]):
    while True:
        await asyncio.sleep(PROGRESS_INTERVAL)
        text = state.progress_text()
        log.info(text.replace("\n", " | "))
        if status_msg is not None:
            try:
                await status_msg.edit(content=text)
            except discord.HTTPException:
                pass

async def scan_guild_live(guild: discord.Guild, params: ScanParams, bot_user_id: int,
                          state: ScanState) -> ScanResult:
    """Прямой сбор без базы данных (как было раньше)."""
    rules = build_rules(guild.id, params.exclude)
    ident = build_identity(guild.id)
    channels = await collect_channels(guild, params, rules, state, bot_user_id)
    state.total_channels = len(channels)
    log.info(
        "[%s] к чтению: %d; исключено: %d; без доступа: %d; пропущено как неактивные: %d",
        guild.name, len(channels), len(state.excluded), len(state.skipped), state.inactive,
    )
    staff = await collect_staff(guild, state, ident)

    # Эмодзи этого сервера: всё, чего здесь нет, считается «внешним»
    try:
        own_emoji_ids = frozenset(e.id for e in await guild.fetch_emojis())
    except discord.HTTPException:
        own_emoji_ids = frozenset(e.id for e in guild.emojis)

    # Ручные антиспам-окна этого сервера (SPAM_FILTER в config.py)
    spam_rules = build_spam_rules(guild.id)

    result = await scan_channels(channels, params, state, own_emoji_ids, spam_rules, ident)
    if state.counted >= 100 and state.text_seen == 0 and not state.cancelled:
        state.notes.append(
            "Текст сообщений недоступен (проверьте Message Content Intent): "
            "данные по эмодзи пустые, это не значит, что их никто не использует."
        )
    if not state.cancelled:
        await collect_audit_live(guild, params, state, result, ident)
    result.staff = staff
    return result

async def scan_guild(guild: discord.Guild, params: ScanParams, bot_user_id: int,
                     state: ScanState, status_msg: Optional[discord.Message] = None) -> ScanResult:
    """Полный сбор по одному серверу. Регистрацией в `running` управляет вызывающий.
    live=True — напрямую из Discord; иначе: синхронизация с базой (если sync=True) и отчёт из базы."""
    progress_task = asyncio.create_task(progress_loop(state, status_msg))
    try:
        if params.live:
            return await scan_guild_live(guild, params, bot_user_id, state)

        rules = build_rules(guild.id, params.exclude)
        ident = build_identity(guild.id)
        spam_rules = build_spam_rules(guild.id)

        if params.sync:
            await sync_guild(guild, params, bot_user_id, state, rules)
            if not state.cancelled:
                await collect_audit(guild, state)

        from dbresult import build_result            # импорт здесь: избегаем циклической зависимости
        state.counted = 0                            # при синхронизации счётчик уже рос; считаем заново по базе
        result = await asyncio.to_thread(build_result, guild.id, params, state, rules, ident, spam_rules)

        if state.counted >= 100 and state.text_seen == 0 and not state.cancelled:
            state.notes.append(
                "Текст сообщений недоступен (проверьте Message Content Intent): "
                "данные по эмодзи пустые, это не значит, что их никто не использует."
            )
        result.staff = await collect_staff(guild, state, ident)
        return result
    finally:
        progress_task.cancel()
        with suppress(asyncio.CancelledError):
            await progress_task

# ---------- Объединение серверов ----------

def _merge_into(dst: ScanResult, src: ScanResult, server: Optional[str]):
    for key, v in src.user_day.items():
        dst.user_day[key] += v
        if server is not None:
            dst.user_server[(key[0], server)] += v
    for key, v in src.user_channel.items():
        dst.user_channel[key] += v
    for key, v in src.heat.items():
        dst.heat[key] += v
    for key, v in src.user_heat.items():
        dst.user_heat[key] += v
    for uid, v in src.replies.items():
        dst.replies[uid] += v
    for key, v in src.reply_pairs.items():
        dst.reply_pairs[key] += v
    for key, v in src.mention_pairs.items():
        dst.mention_pairs[key] += v
    for key, v in src.emoji_user.items():
        dst.emoji_user[key] += v
    for key, v in src.emoji_only_day.items():
        dst.emoji_only_day[key] += v
    for key, info in src.emoji_info.items():
        old = dst.emoji_info.get(key)
        if old is None or (old[2] and not info[2]):
            dst.emoji_info[key] = info
    for key, v in src.reaction_recv.items():
        dst.reaction_recv[key] += v
    for uid, v in src.reaction_msgs.items():
        dst.reaction_msgs[uid] += v
    for key, v in src.reaction_given.items():
        dst.reaction_given[key] += v
    for key, v in src.reaction_pairs.items():
        dst.reaction_pairs[key] += v
    dst.reaction_self_or_bot += src.reaction_self_or_bot
    dst.reaction_unattributed += src.reaction_unattributed
    dst.reaction_capped += src.reaction_capped
    dst.reaction_errors += src.reaction_errors
    dst.reply_unresolved += src.reply_unresolved
    dst.reply_self_or_bot += src.reply_self_or_bot

    # Журнал аудита
    for key, v in src.audit_actions.items():
        dst.audit_actions[key] += v
    for key, v in src.audit_units.items():
        dst.audit_units[key] += v
    for key, v in src.audit_user_action.items():
        dst.audit_user_action[key] += v
    for key, v in src.audit_user_units.items():
        dst.audit_user_units[key] += v
    for key, v in src.audit_day.items():
        dst.audit_day[key] += v
    for uid, nm in src.audit_names.items():
        dst.audit_names.setdefault(uid, nm)
    dst.audit_total += src.audit_total
    dst.audit_skipped += src.audit_skipped
    dst.audit_unknown_actor += src.audit_unknown_actor
    dst.audit_truncated = max(dst.audit_truncated, src.audit_truncated)
    if src.audit_first and (dst.audit_first is None or src.audit_first < dst.audit_first):
        dst.audit_first = src.audit_first
    if src.audit_last and (dst.audit_last is None or src.audit_last > dst.audit_last):
        dst.audit_last = src.audit_last

    for uid, nm in src.names.items():
        dst.names.setdefault(uid, nm)
    for uid, un in src.usernames.items():
        dst.usernames.setdefault(uid, un)
    for uid, url in src.avatars.items():
        dst.avatars.setdefault(uid, url)
    if server is not None:
        for cid, label in src.channel_names.items():
            dst.channel_names[cid] = f"[{server}] {label}"

def merge_results(results):
    """results: список (guild, state, ScanResult). Возвращает (ScanResult, ScanState)."""
    merged = ScanResult()
    merged.merged = True       # в суммарном отчёте ники показываются без оригинального
    modes = {r.reaction_mode for _, _, r in results}
    if len(modes) == 1:
        merged.reaction_mode = next(iter(modes))
    else:                       # режимы разные: полные данные есть не у всех
        merged.reaction_mode = "off" if "off" in modes else "counts"
    total = ScanState()
    have_prev = bool(results) and all(r.prev is not None for _, _, r in results)
    if have_prev:
        merged.prev = ScanResult()
        merged.prev.names = merged.names
        merged.prev.channel_names = merged.channel_names

    used: set[str] = set()
    for g, s, r in results:
        server = g.name if g.name not in used else f"{g.name} ({g.id})"
        used.add(server)
        _merge_into(merged, r, server)
        if r.staff is not None:
            merged.staff_servers[server] = r.staff
        if have_prev:
            _merge_into(merged.prev, r.prev, None)
        total.total_channels += s.total_channels
        total.channels_done += s.channels_done
        total.seen += s.seen
        total.counted += s.counted
        total.inactive += s.inactive
        total.reaction_msgs += s.reaction_msgs
        total.reaction_requests += s.reaction_requests
        total.reaction_errors += s.reaction_errors
        total.reaction_capped = total.reaction_capped or s.reaction_capped
        total.spam_filtered += s.spam_filtered
        total.cancelled = total.cancelled or s.cancelled
        for attr in ("skipped", "failed", "excluded", "unused_rules", "notes"):
            getattr(total, attr).extend(f"[{g.name}] {x}" for x in getattr(s, attr))

    # Статус аудита в сумме: «ok» только если он собран по всем серверам
    audit_modes = {r.audit_mode for _, _, r in results}
    if audit_modes == {"ok"}:
        merged.audit_mode = "ok"
    elif "ok" in audit_modes:
        merged.audit_mode = "partial"
    elif "failed" in audit_modes:
        merged.audit_mode = "failed"
    elif "denied" in audit_modes:
        merged.audit_mode = "denied"
    else:
        merged.audit_mode = "off"
    return merged, total