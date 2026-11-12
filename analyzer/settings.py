"""Настройки автономного анализатора. Имена листов и ключей должны совпадать с reports.py бота."""

from datetime import date

# Видимые листы отчёта бота
SHEET_SUMMARY = "Сводка"
SHEET_DAILY = "По дням (список)"
SHEET_ANALYTICS = "Аналитика"
SHEET_STAFF = "Администрация"
SHEET_INFO = "Служебное"

# Скрытые служебные листы (схема данных 2)
SHEET_DATA_USERS = "data_users"
SHEET_DATA_CHANNELS = "data_channels"
SHEET_DATA_HEAT = "data_heat"
SHEET_DATA_SERVERS = "data_servers"
SHEET_DATA_PREV = "data_prev"
SHEET_DATA_REPLIES = "data_replies"
SHEET_DATA_MENTIONS = "data_mentions"
SHEET_DATA_REPLY_META = "data_reply_meta"
SHEET_DATA_EMOJI = "data_emoji"
SHEET_DATA_EMOJI_INFO = "data_emoji_info"
SHEET_DATA_EMOJI_DAYS = "data_emoji_days"
SHEET_DATA_REACTION_META = "data_reaction_meta"
SHEET_DATA_REACTIONS = "data_reactions"
SHEET_DATA_REACTION_MSGS = "data_reaction_msgs"
SHEET_DATA_REACTION_PAIRS = "data_reaction_pairs"
SHEET_DATA_REACTION_USERS = "data_reaction_users"
SHEET_DATA_AUDIT_META = "data_audit_meta"
SHEET_DATA_AUDIT_USERS = "data_audit_users"
SHEET_DATA_ADMIN_EVENTS = "data_admin_events"

REQUIRED_SHEETS = [SHEET_SUMMARY, SHEET_DAILY, SHEET_INFO]

# Ключи листа «Служебное», которые могут повторяться
MULTI_KEYS = {"Примечание", "⚠ Внимание"}
KIND_BACK = {"Основатель": "owner", "Админ": "admin"}
MERGED_FILE_PREFIX = "Суммарная"
MERGED_TITLE_PREFIX = "Суммарно по серверам"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
KIND_RU = {v: k for k, v in KIND_BACK.items()}
ADMIN_EVENT_LABELS = {
    "admin_grant": "Выдана админская роль",
    "admin_revoke": "Снята админская роль",
    "owner_transfer": "Передача прав Основателя",
}

# ---------- Начало истории серверов ----------
GUILD_START_DATES = {
    1548361478103638068: date(2026, 9, 27),   # Суетологи
    1561089081264316570: date(2026, 10, 18),  # Комитет
}

# ---------- СЗП: степень значимости пользователя (этап 3) ----------
# СЗП = 100 × (V·wV + C·wC + O·wO + G·wG + B·wB), каждый компонент от 0 до 1.
SZP_WEIGHTS = {"V": 0.30, "C": 0.20, "O": 0.20, "G": 0.20, "B": 0.10}
SZP_NAMES = {
    "V": "Объём", "C": "Постоянство", "O": "Отклик", "G": "Управление", "B": "Охват",
}
SZP_LEVELS = [(70, "ключевой"), (50, "значимый"), (25, "заметный"), (0, "рядовой")]

# C — постоянство
SZP_MIN_DAYS = 7          # окно короче (дней от начала сервера) — компонент C не считается
SZP_RECENT_DAYS = 7       # «присутствует сейчас»: активные дни из последних N дней окна
SZP_C_ALL_W = 0.7         # вес доли активных дней (остальное — присутствие в последние дни)

# O — отклик
SZP_O_MIN_MSGS = 20       # меньше сообщений — отклик пропорционально гасится (защита от «молчуна с тегами»)
SZP_O_APPEAL_W = 0.6      # вес входящих обращений (остальное — полученные реакции)
SZP_O_BREADTH_W = 0.5     # доля «широты» (сколько разных людей обращалось) внутри оценки обращений

# G — управление
SZP_G_ROLE_W = 0.5        # вес роли (остальное — реальная работа по журналу аудита)
SZP_ROLE = {"owner": 1.0, "admin": 0.7}
# Неформальные роли, вручную: {ID сервера: {ID пользователя: значение 0..1}}.
# Пример: ник «Bellbrook (Зам Гефеста)» говорит о роли заместителя, но роли администрации у него нет.
# В суммарном файле берётся максимум по всем серверам. Поправьте или удалите на своё усмотрение.
SZP_INFORMAL_ROLE = {
    1548361478103638068: {420213562355482624: 0.5},
}
# Вес действия в журнале аудита (1.0 — по умолчанию). Коды — как в config.py бота (AUDIT_LABELS).
SZP_AUDIT_WEIGHTS = {
    "ban": 3.0, "unban": 3.0, "kick": 3.0, "member_prune": 3.0,       # жёсткая модерация
    "overwrite_create": 0.3, "overwrite_update": 0.3, "overwrite_delete": 0.3,   # массовая мелочь
    "invite_create": 0.3, "invite_update": 0.3, "invite_delete": 0.3,
}
SZP_AUDIT_DEFAULT = 1.0
SZP_AUDIT_IGNORE_PREFIX = "unknown_"      # неопознанные коды Discord не считаются
AUDIT_RETENTION_DAYS = 45                 # столько Discord хранит журнал аудита

# B — охват
SZP_SIG_CHANNEL = 0.02    # значимый канал: даёт >= 2% сообщений сервера
SZP_B_MIN_MSGS = 3        # канал засчитывается, если в нём >= N сообщений человека

# ---------- Риск потери ----------
RISK_WEIGHTS = {"recency": 0.5, "decline": 0.3, "streak": 0.2}
RISK_RECENCY_GAPS = 3     # молчание дольше (1 + N) обычных промежутков = максимум
RISK_DECLINE_RANGE = 0.7  # падение темпа на 70% и больше = максимум
RISK_STREAK_DAYS = 7      # потерянная серия в N дней и больше = максимум
RISK_LEVELS = [(60, "высокий"), (30, "средний"), (0, "низкий")]

# ---------- Отбор для индивидуальных отчётов ----------
SELECT_TOP_N = 20
SELECT_MIN_SZP = 50
MAX_INDIVIDUAL = 60

# ---------- Особая информация о пользователях (пишется вручную) ----------
SPEC_INFO = {
    675694374700580886: "Я хожу в чёрных трусах",
    1270730126405013514: "Ya pidor",
}

# Служебное: те же данные с ID-строками (в анализаторе ID хранятся как строки)
SPEC_INFO_BY_ID = {str(k).strip(): str(v).strip()
                   for k, v in SPEC_INFO.items() if str(v).strip()}