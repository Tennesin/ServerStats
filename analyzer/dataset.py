"""Контракт между источниками данных (xlsx, БД) и остальным анализатором: класс Dataset.
Источники заполняют Dataset, metrics / scoring / charts / report_* читают только его."""

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

USER_COLS = ["uid", "name", "username", "n", "replies", "role", "avatar"]
DAILY_COLS = ["uid", "date", "n"]
CHANNEL_COLS = ["uid", "channel_id", "channel", "n"]
HEAT_COLS = ["uid", "weekday", "hour", "n"]
HEAT_TOTAL_COLS = ["weekday", "hour", "n"]
SERVER_COLS = ["uid", "server", "n"]
STAFF_COLS = ["server", "uid", "name", "kind", "roles", "n"]
REPLY_COLS = ["src", "dst", "n"]
EMOJI_USER_COLS = ["uid", "key", "only", "n"]
EMOJI_INFO_COLS = ["key", "name", "animated", "external"]
EMOJI_DAY_COLS = ["uid", "date", "n"]
REACTION_COLS = ["uid", "key", "n"]
REACTION_MSG_COLS = ["uid", "n"]
AUDIT_USER_COLS = ["uid", "name", "action", "count", "units"]
ADMIN_EVENT_COLS = ["date", "kind", "role", "who", "who2", "server"]

# Новые таблицы (источник: БД)
AUDIT_ACTION_COLS = ["action", "count", "units"]
AUDIT_DAY_COLS = ["date", "count"]
PRESENCE_COLS = ["server", "uid", "present", "joined", "first_seen", "left"]       # joined/first_seen/left — date
MEMBER_ROLE_COLS = ["server", "uid", "name", "username", "is_bot", "top_pos", "roles"]   # roles — список названий
ROLE_COLS = ["server", "role_id", "name", "color", "position", "hoist", "mentionable",
             "managed", "members", "perms", "is_everyone"]                         # perms — frozenset имён прав

class LoaderError(Exception):
    """Данные нельзя использовать (не тот формат, повреждены, нет базы и т. п.)."""

@dataclass
class SyncInfo:
    """Состояние данных одного сервера на момент анализа (только источник «БД»)."""
    last_sync: Optional[datetime] = None        # конец последней завершённой синхронизации
    last_status: str = ""                       # ok / partial / cancelled
    snapshot_at: Optional[datetime] = None      # когда сняты администрация, роли и присутствие
    snapshot_partial: bool = False              # список участников в снимке был неполным
    audit_status: str = ""                      # ok / denied / failed / "" (не собирался)
    audit_truncated: bool = False
    audit_since: Optional[date] = None          # с какого дня журнал аудита копится в базе

@dataclass
class Dataset:
    path: Path                       # у источника «БД» — путь к файлу базы
    title: str
    guild_id: Optional[int]
    is_merged: bool
    schema: int
    full_data: bool                  # есть ли разбивка по каналам, часам и т. д. (у БД всегда True)
    tz: str
    period_text: str
    declared_start: Optional[date]
    declared_end: Optional[date]
    data_start: Optional[date]       # фактически первая и последняя даты с сообщениями
    data_end: Optional[date]
    incomplete: bool                 # сбор был остановлен вручную
    truncated: bool                  # «По дням (список)» обрезан лимитом (только xlsx)
    compare: bool
    meta: dict
    notes: list
    excluded: list
    users: pd.DataFrame
    daily: pd.DataFrame
    prev_daily: pd.DataFrame
    channels: pd.DataFrame
    heat: pd.DataFrame
    heat_total: pd.DataFrame
    servers: pd.DataFrame
    staff: pd.DataFrame
    warnings: list = field(default_factory=list)
    filtered: str = ""
    reply_pairs: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REPLY_COLS))
    reply_unresolved: int = 0
    reply_self_or_bot: int = 0
    mention_pairs: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REPLY_COLS))
    address_pairs: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REPLY_COLS))
    has_emoji: bool = False
    emoji_user: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=EMOJI_USER_COLS))
    emoji_info: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=EMOJI_INFO_COLS))
    emoji_days: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=EMOJI_DAY_COLS))
    # Реакции
    has_reactions: bool = False
    reactions_full: bool = False     # True — известно, кто ставил реакции
    reaction_recv: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REACTION_COLS))
    reaction_given: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REACTION_COLS))
    reaction_msgs: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REACTION_MSG_COLS))
    reaction_pairs: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=REPLY_COLS))
    reaction_names: dict = field(default_factory=dict)
    reaction_self_or_bot: int = 0
    reaction_unattributed: int = 0
    reaction_capped: int = 0
    reaction_errors: int = 0
    # Журнал аудита и начало истории сервера
    has_audit: bool = False
    audit_mode: int = 0              # 1 собран, 2 нет доступа, 3 ошибка, 4 собран не по всем серверам
    audit_users: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=AUDIT_USER_COLS))
    server_start: Optional[date] = None
    admin_events: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=ADMIN_EVENT_COLS))

    # ---- Новое (заполняет источник «БД»; у xlsx остаются значения по умолчанию) ----
    source: str = "xlsx"             # "xlsx" / "db"
    guild_ids: list = field(default_factory=list)       # серверы, вошедшие в набор (в суммарном — несколько)
    audit_actions: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=AUDIT_ACTION_COLS))
    audit_days: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=AUDIT_DAY_COLS))
    audit_total: int = 0
    audit_skipped: int = 0
    audit_unknown_actor: int = 0
    has_presence: bool = False       # есть ли снимок присутствия (иначе «нет на сервере» утверждать нельзя)
    presence: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=PRESENCE_COLS))
    has_roles: bool = False
    member_roles: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=MEMBER_ROLE_COLS))
    role_table: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=ROLE_COLS))
    channel_parent: dict = field(default_factory=dict)  # channel_id (строка) -> ID родительского канала (для веток)
    sync_info: dict = field(default_factory=dict)       # название сервера -> SyncInfo

    @property
    def total_messages(self) -> int:
        return int(self.daily["n"].sum()) if len(self.daily) else 0

    @property
    def window(self) -> tuple[Optional[date], Optional[date]]:
        """Календарное окно анализа: заявленный период, иначе фактические даты."""
        return (self.declared_start or self.data_start, self.declared_end or self.data_end)

    @property
    def window_days(self) -> int:
        a, b = self.window
        return (b - a).days + 1 if a and b else 0

    def present_ids(self) -> Optional[set]:
        """ID людей, которые сейчас на сервере (в суммарном наборе: хотя бы на одном).
        None — снимка присутствия нет, судить об этом нельзя."""
        if not self.has_presence:
            return None
        p = self.presence
        return set(p.loc[p["present"] == 1, "uid"])