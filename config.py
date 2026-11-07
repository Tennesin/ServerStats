"""
Настройки, мелкие утилиты и пресеты периодов. Про Discord этот модуль ничего не знает.

Переменные окружения (или файл .env рядом со скриптом):
  DISCORD_TOKEN    — токен бота (обязательно)
  TIMEZONE         — часовой пояс для границ дней (по умолчанию Asia/Tashkent)
  STATS_DIR        — куда сохранять отчёты (по умолчанию папка output рядом с запуском)
  CONCURRENCY      — сколько каналов читать одновременно (по умолчанию 4)
  EXCLUDE_FILE     — путь к JSON-файлу со списком исключений
"""

import calendar
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # необязательно: чтение .env, если установлен python-dotenv
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- Настройки ----------

# Чаты, которые бот пропускает. Ключ — ID сервера, значение — множество чатов.
# Внутри можно писать и ID канала/ветки (числом), и ТОЧНОЕ название (строкой, без учёта регистра).
# Пустое множество пишется как set(), а не {} (это пустой словарь).
IGNORED_CHATS = {
    1548361478103638068: {   # Суетологи
        1555686019389464668, 1548378402279526510, 1551138756470509598, 1558791241741762560,
        1554857793821872128, 1554900297279868928, 1560031163190353960,
    },
    1561089081264316570: {
        1561326243394359398, 1561357040314749105, 1561356825415254097,
        1561089404422987977, 1561103187572957224,
    },   # Комитет
}

# Роли администрации. Ключ — ID сервера, значение — множество ролей (название строкой или ID числом).
# Основатель (владелец сервера) определяется автоматически и сюда не вписывается.
ADMIN_ROLES = {
    1548361478103638068: {"Суетологи"},
    1561089081264316570: {"Зам. главы"},
}

# Игнорируемые пользователи (бот не учитывает их нигде: ни сообщения, ни ответы, ни реакции, ни аудит).
# Элемент списка:
#   ID                      — игнорировать на ВСЕХ серверах
#   (ID, ID_сервера)        — игнорировать только на этом сервере
IGNORE_USERS = [
    1553301735672643709,
    (675694374700580886, 1548361478103638068),
    # 123456789012345678,
    # (234567890123456789, 1548361478103638068),
]

# Слияние двух аккаунтов в одного человека. Элемент списка:
#   (ID_главного, ID_второго)               — на всех серверах
#   (ID_главного, ID_второго, ID_сервера)   — только на этом сервере
# Главный ID — тот, под которым человек будет числиться в отчёте (колонка «ID»).
# Ник и аватарка показываются от аккаунта, писавшего последним.
# Цепочки работают: (A, B) и (B, C) дадут одного человека A.
MERGE_USERS = [
    (1566179061993308175, 1546274207120367756),
    # (111111111111111111, 222222222222222222),
    # (333333333333333333, 444444444444444444, 1561089081264316570),
]

TOKEN = os.environ.get("DISCORD_TOKEN")
TZ_NAME = os.environ.get("TIMEZONE", "Asia/Tashkent").strip() or "Asia/Tashkent"
STATS_DIR = os.environ.get("STATS_DIR")
# Куда складываются отчёты, собранные за ВСЮ историю
ALLDAY_DIR = os.environ.get("ALLDAY_DIR")
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "4") or 4))
EXCLUDE_FILE = os.environ.get("EXCLUDE_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "excluded_chats.json"
)

# SQLite-база (создаётся автоматически рядом с ботом)
DB_PATH = os.environ.get("DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "stats.db"
)
REACTION_REFRESH_DAYS = 14   # сообщения младше N дней перечитываются при каждой синхронизации (реакции меняются)
SYNC_BATCH = 500             # сколько сообщений накапливать перед записью в базу

DATE_FMT = "%d.%m.%Y"
# Глобальный нижний предел: раньше этой даты не читаем ни один сервер.
MIN_START_DATE = date(2026, 9, 27)
# Официальный день запуска конкретных серверов: {ID сервера: дата}.
GUILD_START_DATES = {
    1548361478103638068: date(2026, 9, 27),   # Суетологи: официальный запуск
    1561089081264316570: date(2026, 10, 18),  # Комитет: официальный запуск
}

NAME_WIDTH = 48
PROGRESS_INTERVAL = 15              # секунд между обновлениями прогресса
UPLOAD_LIMIT = 9 * 1024 * 1024      # с запасом под лимит вложения Discord (10 МБ)
MAX_MATRIX_COLS = 1000              # максимум столбцов в матрице
MAX_LIST_ROWS = 1_000_000           # лимит Excel ~1 048 576 строк на лист

SORT_LABELS = {
    "count": "по количеству сообщений (больше — выше)",
    "name": "по нику (А–Я)",
}
DETAIL_LABELS = {
    "auto": "Авто",
    "days": "По дням",
    "weeks": "По неделям",
    "months": "По месяцам",
}
FORMAT_LABELS = {
    "xlsx": "Excel (xlsx)",
    "txt": "Текст (txt)",
    "both": "Оба файла",
}
REACTION_LABELS = {
    "counts": "Счётчики (быстро, без списка людей)",
    "full": "Полный: кто ставил (медленно)",
    "off": "Не собирать",
}
REACTION_MAX_USERS = 1000        # сколько людей читать максимум у одной реакции
REACTION_MAX_REQUESTS = 30000    # общий потолок запросов «кто ставил» на один сервер за сбор

# ---------- Журнал аудита (действия администрации) ----------
AUDIT_ENABLED = True               # False — журнал аудита не собирается и лист «Аудит» не создаётся
AUDIT_MAX_ENTRIES = 200_000        # предохранитель: сколько записей максимум читать на один сервер
AUDIT_RETENTION_DAYS = 45          # сколько Discord хранит журнал (для предупреждения о неполном периоде)

# Действия, которые не считаются действиями администрации (код действия, как в discord.AuditLogAction.name).
# Автомодерация пишет в журнал нарушителя как «автора», поэтому по умолчанию отсекается.
AUDIT_SKIP_ACTIONS = {
    "automod_block_message",
    "automod_flag_message",
    "automod_timeout_member",
}

# Русские названия действий. Неизвестные коды в отчёте показываются как есть.
AUDIT_LABELS = {
    "guild_update": "Изменён сервер",
    "channel_create": "Создан канал",
    "channel_update": "Изменён канал",
    "channel_delete": "Удалён канал",
    "overwrite_create": "Созданы права канала",
    "overwrite_update": "Изменены права канала",
    "overwrite_delete": "Удалены права канала",
    "kick": "Кик участника",
    "member_prune": "Удаление неактивных участников",
    "ban": "Бан участника",
    "unban": "Разбан участника",
    "member_update": "Изменён участник (ник, тайм-аут и т. п.)",
    "member_role_update": "Изменены роли участника",
    "member_move": "Перемещён в голосовой канал",
    "member_disconnect": "Отключён от голосового канала",
    "bot_add": "Добавлен бот",
    "role_create": "Создана роль",
    "role_update": "Изменена роль",
    "role_delete": "Удалена роль",
    "invite_create": "Создано приглашение",
    "invite_update": "Изменено приглашение",
    "invite_delete": "Удалено приглашение",
    "webhook_create": "Создан вебхук",
    "webhook_update": "Изменён вебхук",
    "webhook_delete": "Удалён вебхук",
    "emoji_create": "Создан эмодзи",
    "emoji_update": "Изменён эмодзи",
    "emoji_delete": "Удалён эмодзи",
    "message_delete": "Удалено сообщение",
    "message_bulk_delete": "Массовое удаление сообщений",
    "message_pin": "Закреплено сообщение",
    "message_unpin": "Откреплено сообщение",
    "integration_create": "Создана интеграция",
    "integration_update": "Изменена интеграция",
    "integration_delete": "Удалена интеграция",
    "stage_instance_create": "Создана трибуна",
    "stage_instance_update": "Изменена трибуна",
    "stage_instance_delete": "Удалена трибуна",
    "sticker_create": "Создан стикер",
    "sticker_update": "Изменён стикер",
    "sticker_delete": "Удалён стикер",
    "scheduled_event_create": "Создано событие",
    "scheduled_event_update": "Изменено событие",
    "scheduled_event_delete": "Удалено событие",
    "thread_create": "Создана ветка",
    "thread_update": "Изменена ветка",
    "thread_delete": "Удалена ветка",
    "app_command_permission_update": "Изменены права команд приложения",
    "automod_rule_create": "Создано правило автомодерации",
    "automod_rule_update": "Изменено правило автомодерации",
    "automod_rule_delete": "Удалено правило автомодерации",
    "soundboard_sound_create": "Создан звук саундборда",
    "soundboard_sound_update": "Изменён звук саундборда",
    "soundboard_sound_delete": "Удалён звук саундборда",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("statbot")

def load_tz(name: str) -> tzinfo:
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise SystemExit(
            f"Неизвестный часовой пояс TIMEZONE={name!r}. "
            "Пример: Asia/Tashkent. На Windows сначала выполните: pip install tzdata"
        )

TZ = load_tz(TZ_NAME)

# ---------- Вспомогательные функции ----------

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean(text) -> str:
    """Убирает управляющие символы, недопустимые в xlsx."""
    return _CTRL_RE.sub("", str(text))

def parse_date(text: str) -> Optional[date]:
    try:
        return datetime.strptime(text.strip(), DATE_FMT).date()
    except ValueError:
        return None

def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")

def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"

def fit_name(name: str) -> str:
    return name if len(name) <= NAME_WIDTH else name[: NAME_WIDTH - 1] + "…"

def fmt_list(items: list, limit: int = 30) -> str:
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" … и ещё {len(items) - limit}"

def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:100] or "server"

def save_local_copy(filename: str, data: bytes, folder: Optional[str] = None) -> str:
    folder = folder or STATS_DIR or "output"
    os.makedirs(folder, exist_ok=True)
    path = os.path.abspath(os.path.join(folder, os.path.basename(filename)))
    with open(path, "wb") as f:
        f.write(data)
    return path

# ---------- Пресеты периодов ----------
# Правило: во всех пресетах учитываются только ЗАВЕРШЁННЫЕ сутки по времени TZ,
# то есть последний день периода — не позже вчерашнего.

PERIOD_PRESETS = {
    "yesterday": "вчера",
    "this-week": "текущая неделя (до вчера)",
    "prev-week": "прошлая неделя",
    "this-month": "текущий месяц (до вчера)",
    "prev-month": "прошлый месяц",
    "this-year": "текущий год (до вчера)",
    "prev-year": "прошлый год",
    "last7d": "последние 7 дней",
    "last30d": "последние 30 дней",
    "last90d": "последние 90 дней",
    "last3m": "последние 3 месяца",
    "last6m": "последние 6 месяцев",
    "last1y": "последний 1 год",
}

_LAST_RE = re.compile(r"^last(\d{1,3})([dwmy])$")
_UNIT_RU = {"d": "дн.", "w": "нед.", "m": "мес.", "y": "г."}

def today_local() -> date:
    """Сегодняшняя дата по часовому поясу TIMEZONE (а не по часам компьютера)."""
    return datetime.now(TZ).date()

def shift_months(d: date, months: int) -> date:
    y, m0 = divmod(d.year * 12 + d.month - 1 + months, 12)
    m = m0 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

def normalize_period_key(text: str) -> str:
    key = re.sub(r"[\s_]+", "", str(text).strip().lower())
    return key.replace("last-", "last")

def resolve_period(text: str, today: Optional[date] = None):
    """Возвращает (start, end, key, label); обе даты включительно.
    Бросает ValueError с понятным сообщением."""
    key = normalize_period_key(text)
    today = today or today_local()
    yday = today - timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    first_month = today.replace(day=1)
    first_year = today.replace(month=1, day=1)

    m = _LAST_RE.match(key)
    if key == "yesterday":
        s, e = yday, yday
    elif key == "this-week":
        s, e = monday, yday
    elif key == "prev-week":
        s, e = monday - timedelta(days=7), monday - timedelta(days=1)
    elif key == "this-month":
        s, e = first_month, yday
    elif key == "prev-month":
        e = first_month - timedelta(days=1)
        s = e.replace(day=1)
    elif key == "this-year":
        s, e = first_year, yday
    elif key == "prev-year":
        e = first_year - timedelta(days=1)
        s = e.replace(month=1, day=1)
    elif m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            raise ValueError("В пресете lastN число должно быть не меньше 1.")
        e = yday
        try:
            if unit == "d":
                s = yday - timedelta(days=n - 1)
            elif unit == "w":
                s = yday - timedelta(days=n * 7 - 1)
            else:
                s = shift_months(today, -n * (12 if unit == "y" else 1))
        except (ValueError, OverflowError):
            raise ValueError("Слишком большой период.")
    else:
        raise ValueError(
            f"Неизвестный период «{text}». Доступно: {', '.join(PERIOD_PRESETS)} "
            "или lastNd / lastNw / lastNm / lastNy (например last45d)."
        )

    if m:
        label = f"последние {m.group(1)} {_UNIT_RU[m.group(2)]}"
    else:
        label = PERIOD_PRESETS[key]
    if s > e:
        raise ValueError(f"В периоде «{label}» пока нет завершённых суток.")
    return s, e, key, label

def build_period(period: Optional[str], start_text: Optional[str],
                 end_text: Optional[str], compare: bool = False):
    """Единая точка разбора периода для /stats и терминала."""
    if period and (start_text or end_text):
        raise ValueError("Укажите либо пресет периода, либо даты начала/конца — не одновременно.")
    yday = today_local() - timedelta(days=1)
    if period:
        s, e, key, label = resolve_period(period)
    else:
        s = e = None
        key = label = ""
        if not start_text and not end_text:
            key, label = "all", "вся история"
        if start_text:
            s = parse_date(start_text)
            if s is None:
                raise ValueError("Неверный формат даты начала. Используйте дд.мм.гггг")
        if end_text:
            e = parse_date(end_text)
            if e is None:
                raise ValueError("Неверный формат даты конца. Используйте дд.мм.гггг")
            if e > yday:
                raise ValueError(
                    f"Дата конца не может быть позже вчерашней ({yday.strftime(DATE_FMT)}): "
                    "текущие сутки ещё не завершены."
                )
        else:
            e = yday                    # конец не задан -> последние завершённые сутки
        if s and s > e:
            raise ValueError(
                "Дата начала позже даты конца." if end_text else
                f"Дата начала позже последних завершённых суток ({yday.strftime(DATE_FMT)})."
            )
    if e < MIN_START_DATE:
        raise ValueError(
            f"Период целиком раньше запуска сервера ({MIN_START_DATE.strftime(DATE_FMT)}) "
            "или в нём ещё нет завершённых суток — данных нет."
        )
    if s is None or s < MIN_START_DATE:
        s = MIN_START_DATE          # «с начала истории» и слишком ранние даты подрезаются

    if compare:
        length = (e - s).days + 1
        if s - timedelta(days=length) < MIN_START_DATE:
            raise ValueError(
                "Для сравнения предыдущий период такой же длины начался бы раньше "
                f"{MIN_START_DATE.strftime(DATE_FMT)} — сравнивать не с чем."
            )
    return s, e, key, label

# ---------- Папки отчётов: год-месяц / сборник ----------

def period_folder_name(start: Optional[date], end: Optional[date], with_year: bool = False) -> str:
    """«26-10» для одного дня, «18-10 - 25-10» для периода."""
    fmt = "%d-%m-%Y" if with_year else "%d-%m"
    if start and end and start != end:
        return f"{start.strftime(fmt)} - {end.strftime(fmt)}"
    d = end or start or today_local()
    return d.strftime(fmt)

def collection_dir(start: Optional[date], end: Optional[date],
                   base: Optional[str] = None, unique: bool = False,
                   full_history: bool = False) -> str:
    """Путь к папке сборника."""
    if full_history:
        path = os.path.join(ALLDAY_DIR, period_folder_name(start, end, with_year=True))
    else:
        base = base or STATS_DIR or "output"
        ref = end or start or today_local()
        path = os.path.join(base, ref.strftime("%Y-%m"), period_folder_name(start, end))
    if unique:
        cand, i = path, 2
        while os.path.exists(cand):
            cand = f"{path} ({i})"
            i += 1
        path = cand
    return os.path.abspath(path)

# Ручной фильтр спама по часам. Ключ — ID сервера, значение — список правил.
# Правило: (эмодзи, "дд.мм.гггг", час_начала, час_конца)
#   • эмодзи: ID кастомного эмодзи (число) ИЛИ обычный Unicode-символ строкой, например "💀"
#   • даты и часы — по времени TIMEZONE; час_начала включительно, час_конца НЕ включительно
#     (14, 16) = с 14:00 до 15:59; допустимо 0..24
SPAM_FILTER = {
    1548361478103638068: [
        # (123456789012345678, "05.10.2026", 14, 16),
    ],
    1561089081264316570: [
        (1561586027859152917, "27.10.2026", 17, 24),
    ],
}