"""Снимок «живых» данных сервера (администрация, роли, присутствие) в базу."""

import json
import time
from collections import defaultdict

import discord

from config import ADMIN_ROLES, log
from db import get_db, set_meta
from rules import looks_like_id, norm_name

_UPSERT_PRESENCE = (
    "INSERT INTO presence (guild_id, uid, present, joined_ts, first_seen_ts, last_seen_ts, left_ts) "
    "VALUES (?, ?, 1, ?, ?, ?, NULL) "
    "ON CONFLICT(guild_id, uid) DO UPDATE SET present=1, "
    "joined_ts=COALESCE(excluded.joined_ts, presence.joined_ts), "
    "last_seen_ts=excluded.last_seen_ts, left_ts=NULL"
)

def _admin_role_ids(guild: discord.Guild) -> set:
    """ID ролей сервера, которые входят в ADMIN_ROLES (по ID или по названию без учёта регистра)."""
    wanted_ids, wanted_names = set(), set()
    for item in ADMIN_ROLES.get(guild.id, ()):
        if isinstance(item, int) or looks_like_id(str(item)):
            wanted_ids.add(int(item))
        elif norm_name(item):
            wanted_names.add(norm_name(item))
    return {r.id for r in guild.roles
            if not r.is_default() and (r.id in wanted_ids or norm_name(r.name) in wanted_names)}

async def _save(guild: discord.Guild):
    db = get_db()
    gid = guild.id

    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            log.warning("[%s] Не удалось полностью загрузить участников для снимка: %s", guild.name, e)
    members = list(guild.members)
    owner_id = guild.owner_id
    if owner_id and not any(m.id == owner_id for m in members):
        try:
            members.append(await guild.fetch_member(owner_id))    # владельца может не быть в кэше
        except discord.HTTPException:
            pass

    # Если участники загрузились не все, ничего не удаляем и никого не помечаем «ушедшим»
    partial = bool(guild.member_count) and len(members) < guild.member_count
    now = int(time.time())
    admin_ids = _admin_role_ids(guild)

    staff, presence, member_rows = [], [], []
    counts: dict = defaultdict(int)
    for m in members:
        roles = sorted((r for r in m.roles if not r.is_default()), key=lambda r: -r.position)
        for r in roles:
            counts[r.id] += 1
        member_rows.append((gid, m.id, m.display_name, m.name, int(m.bot),
                            roles[0].position if roles else -1,
                            json.dumps([r.name for r in roles], ensure_ascii=False)))
        if m.bot:
            continue
        joined = int(m.joined_at.timestamp()) if m.joined_at else None
        presence.append((gid, m.id, joined, now, now))

        admin_names = [r.name for r in roles if r.id in admin_ids]
        if m.id == owner_id:
            kind = "owner"
        elif admin_names:
            kind = "admin"
        else:
            continue
        staff.append((gid, m.id, m.display_name, m.name, kind,
                      json.dumps(admin_names, ensure_ascii=False)))
    if owner_id and not any(row[1] == owner_id for row in staff):
        staff.append((gid, owner_id, "", "", "owner", "[]"))   # владелец известен только по ID

    role_rows = []
    for r in guild.roles:
        perms = ",".join(name for name, value in r.permissions if value)
        role_rows.append((gid, r.id, r.name, r.color.value, r.position, int(r.hoist),
                          int(r.mentionable), int(r.managed), perms, counts.get(r.id, 0),
                          int(r.is_default())))

    with db:                                                    # одна транзакция: снимок целиком или никак
        # Присутствие: временная метка present=2 отделяет «увидели сейчас» от «не увидели»
        db.execute("UPDATE presence SET present=2 WHERE guild_id=? AND present=1", (gid,))
        db.executemany(_UPSERT_PRESENCE, presence)
        if partial:
            db.execute("UPDATE presence SET present=1 WHERE guild_id=? AND present=2", (gid,))
        else:
            db.execute("UPDATE presence SET present=0, left_ts=? WHERE guild_id=? AND present=2",
                       (now, gid))

        if not partial:
            db.execute("DELETE FROM staff_snapshot WHERE guild_id=?", (gid,))
            db.execute("DELETE FROM member_roles WHERE guild_id=?", (gid,))
        db.executemany("INSERT OR REPLACE INTO staff_snapshot VALUES (?,?,?,?,?,?)", staff)
        db.executemany("INSERT OR REPLACE INTO member_roles VALUES (?,?,?,?,?,?,?)", member_rows)

        db.execute("DELETE FROM role_snapshot WHERE guild_id=?", (gid,))
        db.executemany("INSERT INTO role_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)", role_rows)

    set_meta(db, f"snapshot_ts:{gid}", now)
    set_meta(db, f"guild_name:{gid}", guild.name)
    set_meta(db, f"snapshot_partial:{gid}", "1" if partial else "0")
    log.info("[%s] снимок сохранён: участников %d, ролей %d, администрации %d%s",
             guild.name, len(presence), len(role_rows), len(staff),
             " (список участников неполный)" if partial else "")

async def save_snapshots(guild: discord.Guild, state) -> None:
    """Вызывается после синхронизации. Любой сбой только пишется в примечания, синхронизацию не ломает."""
    try:
        await _save(guild)
    except Exception:
        log.exception("[%s] Не удалось сохранить снимок администрации, ролей и присутствия", guild.name)
        state.notes.append("Снимок администрации, ролей и присутствия не сохранён "
                           "(подробности в консоли бота).")