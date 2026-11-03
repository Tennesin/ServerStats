"""SQLite-хранилище: схема и подключение."""

import os
import sqlite3

from config import DB_PATH, TZ_NAME, log

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS channels (
    channel_id     INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    parent_id      INTEGER,
    name           TEXT NOT NULL,
    label          TEXT NOT NULL,
    last_synced_id INTEGER NOT NULL DEFAULT 0   -- водяной знак: последнее обработанное сообщение
);

CREATE TABLE IF NOT EXISTS users (
    guild_id     INTEGER NOT NULL,
    uid          INTEGER NOT NULL,
    display_name TEXT,
    username     TEXT,
    avatar_url   TEXT,
    is_bot       INTEGER NOT NULL DEFAULT 0,
    last_seen_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, uid)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id  INTEGER PRIMARY KEY,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    ts          INTEGER NOT NULL,       -- UTC, секунды
    day         TEXT NOT NULL,          -- локальная дата YYYY-MM-DD (по TIMEZONE)
    hour        INTEGER NOT NULL,
    weekday     INTEGER NOT NULL,
    is_reply    INTEGER NOT NULL,
    reply_to    INTEGER,                -- автор исходного сообщения (если известен)
    reply_state INTEGER NOT NULL,       -- 0 не ответ, 1 адресат известен, 2 неизвестен
    has_text    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_day ON messages (guild_id, day);

CREATE TABLE IF NOT EXISTS mentions (
    message_id INTEGER NOT NULL,
    target_id  INTEGER NOT NULL,
    PRIMARY KEY (message_id, target_id)
);

CREATE TABLE IF NOT EXISTS emoji (
    message_id INTEGER NOT NULL,
    key        TEXT NOT NULL,           -- "c:<id>" или "u:<символ>"
    only       INTEGER NOT NULL,        -- сообщение состоит только из эмодзи
    PRIMARY KEY (message_id, key)
);

CREATE TABLE IF NOT EXISTS emoji_info (
    guild_id INTEGER NOT NULL,
    key      TEXT NOT NULL,
    name     TEXT,
    animated INTEGER NOT NULL,
    external INTEGER NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL,
    key        TEXT NOT NULL,
    count      INTEGER NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,   -- сколько людей реально прочитано (режим «полный»)
    PRIMARY KEY (message_id, key)
);

CREATE TABLE IF NOT EXISTS reaction_users (
    message_id INTEGER NOT NULL,
    key        TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    PRIMARY KEY (message_id, key, user_id)
);

CREATE TABLE IF NOT EXISTS audit_entries (
    entry_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    day      TEXT NOT NULL,
    action   TEXT NOT NULL,
    user_id  INTEGER NOT NULL,            -- 0 — автор неизвестен
    units    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_day ON audit_entries (guild_id, day);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    started_ts     INTEGER NOT NULL,
    finished_ts    INTEGER,
    status         TEXT,                  -- ok / partial / cancelled
    reactions_mode TEXT,
    messages_read  INTEGER
);
"""

_con = None

def get_db() -> sqlite3.Connection:
    """Основное подключение (только из потока бота)."""
    global _con
    if _con is not None:
        return _con
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    row = con.execute("SELECT value FROM meta WHERE key='tz'").fetchone()
    if row is None:
        con.execute("INSERT INTO meta VALUES ('tz', ?)", (TZ_NAME,))
        con.commit()
    elif row[0] != TZ_NAME:
        raise SystemExit(
            f"База создана с TIMEZONE={row[0]}, а сейчас задан {TZ_NAME}. "
            "Даты в базе хранятся по локальному времени, поэтому смена пояса испортит данные."
        )
    _con = con
    log.info("База данных: %s", DB_PATH)
    return con

def new_connection() -> sqlite3.Connection:
    """Отдельное подключение для фонового потока (сборка отчёта). Вызывать после get_db()."""
    get_db()                                  # гарантирует, что база и схема существуют
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA query_only=ON")
    return con

def get_meta(con, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_meta(con, key: str, value):
    con.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
    con.commit()