"""Правила отбора данных: исключённые чаты, игнор и слияние аккаунтов, антиспам-окна эмодзи.
Модуль не зависит от Discord, поэтому его используют и бот, и анализатор."""

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from config import EXCLUDE_FILE, IGNORED_CHATS, IGNORE_USERS, MERGE_USERS, SPAM_FILTER, log, parse_date
from emoji_utils import normalize_unicode

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
        """ch — любой объект с полями id и name (канал discord.py или SimpleNamespace)."""
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

def build_identity(guild_id: Optional[int]) -> IdentityRules:
    """guild_id=None — правила для суммарного отчёта: берутся только записи «на всех серверах»."""
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