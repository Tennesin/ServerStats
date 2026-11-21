"""Этап 4: чтение данных напрямую из базы бота (stats.db) в Dataset."""
import botroot

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import pandas as pd

import settings as S
from config import (
    AUDIT_ENABLED, AUDIT_RETENTION_DAYS, AUDIT_SKIP_ACTIONS, DATE_FMT, DB_PATH, TZ, TZ_NAME,
    build_period, guild_start_date_by_id, period_folder_name,
)
from dataset import (
    ADMIN_EVENT_COLS, AUDIT_ACTION_COLS, AUDIT_DAY_COLS, AUDIT_USER_COLS, CHANNEL_COLS, DAILY_COLS,
    EMOJI_DAY_COLS, EMOJI_INFO_COLS, EMOJI_USER_COLS, HEAT_COLS, HEAT_TOTAL_COLS, MEMBER_ROLE_COLS,
    PRESENCE_COLS, REACTION_COLS, REACTION_MSG_COLS, REPLY_COLS, ROLE_COLS, SERVER_COLS, STAFF_COLS,
    USER_COLS, Dataset, LoaderError, SyncInfo,
)
from rules import build_identity, build_rules, build_spam_rules

# ---------- Мелкие помощники ----------

def _q(db, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, db, params=params)

def _has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def _meta(db, key: str):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None

def _empty(cols: list, date_col: Optional[str] = None) -> pd.DataFrame:
    df = pd.DataFrame(columns=cols)
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col])
    return df

def _canon(s: pd.Series, ident) -> pd.Series:
    """Слияние аккаунтов: ID второго аккаунта -> ID главного."""
    return s.replace(ident.aliases) if ident.aliases else s

def _sum(frames: list, keys: list, cols: list, date_col: Optional[str] = None) -> pd.DataFrame:
    frames = [f for f in frames if len(f)]
    if not frames:
        return _empty(cols, date_col)
    out = pd.concat(frames, ignore_index=True).groupby(keys, as_index=False)["n"].sum()
    return out[cols]

def _spam_mask(df: pd.DataFrame, spam: dict) -> pd.Series:
    """Строки, попавшие в ручные антиспам-окна (нужны колонки key, day, hour)."""
    mask = pd.Series(False, index=df.index)
    for key, wins in spam.items():
        k = df["key"] == key
        if not k.any():
            continue
        for d, h1, h2 in wins:
            mask |= k & (df["day"] == d.isoformat()) & (df["hour"] >= h1) & (df["hour"] < h2)
    return mask

def _ts_date(v) -> Optional[date]:
    return None if pd.isna(v) else datetime.fromtimestamp(int(v), TZ).date()

def _period_text(label: str, s: date, e: date) -> str:
    rng = f"{s.strftime(DATE_FMT)} — {e.strftime(DATE_FMT)}"
    return f"{label} ({rng})" if label else rng

def _roles_map(staff: pd.DataFrame, merged: bool) -> dict:
    out: dict = {}
    for r in staff.itertuples():
        text = S.KIND_RU.get(r.kind, r.kind) + (f" ({r.server})" if merged else "")
        out.setdefault(r.uid, []).append(text)
    return {u: ", ".join(v) for u, v in out.items()}

# ---------- Один сервер ----------

def _load_guild(db, path: Path, gid: int, title: str, s: date, e: date, compare: bool,
                rx_opt: str, pre_notes: list) -> Dataset:
    notes, warnings = list(pre_notes), []
    ident, rules, spam = build_identity(gid), build_rules(gid, ""), build_spam_rules(gid)
    ign = list(ident.ignored)
    length = (e - s).days + 1
    lo_d = s - timedelta(days=length) if compare else s
    lo, mid, hi = lo_d.isoformat(), s.isoformat(), e.isoformat()
    spam_filtered = 0

    # ---- каналы и исключения ----
    ch = _q(db, "SELECT channel_id, parent_id, name, label FROM channels WHERE guild_id=?", (gid,))
    excl = {int(r.channel_id) for r in ch.itertuples()
            if rules.matches(SimpleNamespace(id=int(r.channel_id), name=r.name))}
    excl |= {int(r.channel_id) for r in ch.itertuples()                    # ветки исключённых каналов
             if pd.notna(r.parent_id) and int(r.parent_id) in excl}
    excluded = [r.label for r in ch.itertuples() if int(r.channel_id) in excl]
    labels = {int(r.channel_id): r.label for r in ch.itertuples()}
    channel_parent = {str(int(r.channel_id)): int(r.parent_id) for r in ch.itertuples() if pd.notna(r.parent_id)}
    n_rules = len(rules.names) + len(rules.ids)
    if n_rules and not excluded:
        notes.append(f"Заданы правила исключения чатов ({n_rules}): такие каналы бот не читает вообще, "
                     "поэтому в базе их нет и в отчёте они не перечислены.")

    # ---- люди ----
    ur = _q(db, "SELECT uid, display_name, username, avatar_url, is_bot, last_seen_ts FROM users WHERE guild_id=?", (gid,))
    info = {int(r.uid): (r.display_name or "", r.username or "", r.avatar_url or "",
                         int(r.is_bot), int(r.last_seen_ts or 0)) for r in ur.itertuples()}
    bots = {u for u, v in info.items() if v[3]}
    names, unames, avatars, ts_of = {}, {}, {}, {}
    for u, (disp, uname, av, is_bot, ts) in info.items():
        if is_bot or u in ident.ignored:
            continue
        c = ident.canon(u)
        if c not in ts_of or ts >= ts_of[c]:                 # показываем аккаунт, писавший позже
            ts_of[c], names[c], unames[c], avatars[c] = ts, disp or str(c), uname, av

    # Имена тех, кого нет в таблице users (их только тегали или им отвечали): берём из снимка участников
    if _has_table(db, "member_roles"):
        for r in _q(db, "SELECT uid, display_name, username, is_bot FROM member_roles WHERE guild_id=?",
                    (gid,)).itertuples():
            u = int(r.uid)
            if r.is_bot or u in ident.ignored:
                continue
            c = ident.canon(u)
            if c not in names or names[c] == str(c):
                names[c] = r.display_name or r.username or str(c)
                unames[c] = r.username or unames.get(c, "")

    def person(raw: int) -> str:
        v = info.get(raw)
        return (v[0] if v and v[0] else None) or names.get(ident.canon(raw)) or str(ident.canon(raw))

    # ---- сообщения ----
    msg = _q(db, "SELECT message_id, author_id, channel_id, day, hour, weekday, is_reply, reply_to, "
                 "reply_state, has_text FROM messages WHERE guild_id=? AND day BETWEEN ? AND ?",
             (gid, lo, hi))
    msg["reply_to"] = msg["reply_to"].fillna(0).astype("int64")
    msg = msg[~msg["channel_id"].isin(excl) & ~msg["author_id"].isin(ign)].copy()
    msg["uid"] = _canon(msg["author_id"], ident)
    cur, prev = msg[msg["day"] >= mid], msg[msg["day"] < mid]
    counts = cur.groupby("uid").size()
    text_seen = int(cur["has_text"].sum())

    def daily_of(df):
        if df.empty:
            return _empty(DAILY_COLS, "date")
        g = df.groupby(["uid", "day"]).size().reset_index(name="n")
        g["uid"], g["date"] = g["uid"].astype(str), pd.to_datetime(g["day"])
        return g[DAILY_COLS]

    daily, prev_daily = daily_of(cur), daily_of(prev)

    if cur.empty:
        channels, heat = _empty(CHANNEL_COLS), _empty(HEAT_COLS)
    else:
        channels = cur.groupby(["uid", "channel_id"]).size().reset_index(name="n")
        channels["channel"] = channels["channel_id"].map(labels).fillna(channels["channel_id"].astype(str))
        channels["uid"], channels["channel_id"] = channels["uid"].astype(str), channels["channel_id"].astype(str)
        channels = channels[CHANNEL_COLS]
        heat = cur.groupby(["uid", "weekday", "hour"]).size().reset_index(name="n")
        heat["uid"] = heat["uid"].astype(str)
        heat = heat[HEAT_COLS]
    heat_total = (heat.groupby(["weekday", "hour"], as_index=False)["n"].sum()
                  if len(heat) else _empty(HEAT_TOTAL_COLS))
    replies = cur.groupby("uid")["is_reply"].sum()

    # ---- ответы ----
    rep = cur[cur["is_reply"] == 1]
    unresolved = int((rep["reply_state"] != 1).sum())
    rs = rep[(rep["reply_state"] == 1) & ~rep["reply_to"].isin(ign)].copy()
    rs["dst"] = _canon(rs["reply_to"], ident)
    bad = rs["reply_to"].isin(bots) | (rs["dst"] == rs["uid"])
    self_or_bot = int(bad.sum())
    good = rs[~bad]
    if good.empty:
        reply_pairs = _empty(REPLY_COLS)
    else:
        reply_pairs = good.groupby(["uid", "dst"]).size().reset_index(name="n")
        reply_pairs.columns = REPLY_COLS
        reply_pairs[["src", "dst"]] = reply_pairs[["src", "dst"]].astype(str)

    # ---- теги ----
    mq = _q(db, "SELECT m.author_id, m.channel_id, m.reply_to, t.target_id FROM mentions t "
                "JOIN messages m ON m.message_id = t.message_id "
                "WHERE m.guild_id=? AND m.day >= ? AND m.day <= ?", (gid, mid, hi))
    mq["reply_to"] = mq["reply_to"].fillna(0).astype("int64")
    mq = mq[~mq["channel_id"].isin(excl) & ~mq["author_id"].isin(ign)
            & ~mq["target_id"].isin(ign) & ~mq["target_id"].isin(bots)].copy()
    mq["uid"], mq["t"] = _canon(mq["author_id"], ident), _canon(mq["target_id"], ident)
    mq["rd"] = _canon(mq["reply_to"], ident).where(mq["reply_to"] != 0, -1)
    mq = mq[(mq["t"] != mq["uid"]) & (mq["t"] != mq["rd"])]
    if mq.empty:
        mention_pairs = _empty(REPLY_COLS)
    else:
        mention_pairs = mq.groupby(["uid", "t"]).size().reset_index(name="n")
        mention_pairs.columns = REPLY_COLS
        mention_pairs[["src", "dst"]] = mention_pairs[["src", "dst"]].astype(str)
    address_pairs = _sum([reply_pairs, mention_pairs], ["src", "dst"], REPLY_COLS)

    # ---- эмодзи в тексте ----
    eq = _q(db, "SELECT m.message_id, m.author_id, m.channel_id, m.day, m.hour, e.key, e.only FROM emoji e "
                "JOIN messages m ON m.message_id = e.message_id "
                "WHERE m.guild_id=? AND m.day >= ? AND m.day <= ?", (gid, mid, hi))
    eq = eq[~eq["channel_id"].isin(excl) & ~eq["author_id"].isin(ign)].copy()
    eq["uid"] = _canon(eq["author_id"], ident)
    sm = _spam_mask(eq, spam)
    spam_filtered += int(sm.sum())
    eq = eq[~sm]
    if eq.empty:
        emoji_user, emoji_days = _empty(EMOJI_USER_COLS), _empty(EMOJI_DAY_COLS, "date")
    else:
        emoji_user = eq.groupby(["uid", "key", "only"]).size().reset_index(name="n")
        emoji_user["uid"] = emoji_user["uid"].astype(str)
        emoji_user = emoji_user[EMOJI_USER_COLS]
        od = eq[eq["only"] == 1].drop_duplicates("message_id")          # «только эмодзи»: одно сообщение один раз
        if od.empty:
            emoji_days = _empty(EMOJI_DAY_COLS, "date")
        else:
            emoji_days = od.groupby(["uid", "day"]).size().reset_index(name="n")
            emoji_days["uid"], emoji_days["date"] = emoji_days["uid"].astype(str), pd.to_datetime(emoji_days["day"])
            emoji_days = emoji_days[EMOJI_DAY_COLS]
    ei = _q(db, "SELECT key, name, animated, external FROM emoji_info WHERE guild_id=?", (gid,))
    ei["name"] = ei["name"].fillna("")
    emoji_info = ei[EMOJI_INFO_COLS] if len(ei) else _empty(EMOJI_INFO_COLS)
    has_emoji = text_seen > 0
    if int(counts.sum()) >= 100 and text_seen == 0:
        warnings.append("Текст сообщений в базе отсутствует (проверьте Message Content Intent): "
                        "данные по эмодзи пустые, раздел скрыт.")

    # ---- реакции ----
    has_rx = (db.execute("SELECT 1 FROM reactions r JOIN messages m ON m.message_id=r.message_id "
                         "WHERE m.guild_id=? LIMIT 1", (gid,)).fetchone() is not None
              or db.execute("SELECT 1 FROM sync_runs WHERE guild_id=? AND reactions_mode IN ('counts','full') LIMIT 1",
                            (gid,)).fetchone() is not None)
    has_users = db.execute("SELECT 1 FROM reaction_users ru JOIN messages m ON m.message_id=ru.message_id "
                           "WHERE m.guild_id=? LIMIT 1", (gid,)).fetchone() is not None
    full = has_users and rx_opt != "counts"
    if rx_opt == "full" and not has_users:
        notes.append("В базе нет списков людей, ставивших реакции (они собираются только при синхронизации "
                     "в режиме «полный»): реакции показаны как «счётчики».")
    r_recv, r_given, r_pairs = _empty(REACTION_COLS), _empty(REACTION_COLS), _empty(REPLY_COLS)
    r_msgs, r_names = _empty(REACTION_MSG_COLS), {}
    r_self_bot = r_unattr = 0
    if has_rx:
        rq = _q(db, "SELECT r.message_id, m.author_id, m.channel_id, m.day, m.hour, r.key, r.count, r.fetched "
                    "FROM reactions r JOIN messages m ON m.message_id = r.message_id "
                    "WHERE m.guild_id=? AND m.day >= ? AND m.day <= ?", (gid, mid, hi))
        rq = rq[~rq["channel_id"].isin(excl) & ~rq["author_id"].isin(ign)].copy()
        rq["uid"] = _canon(rq["author_id"], ident)
        sm = _spam_mask(rq, spam)
        spam_filtered += int(rq.loc[sm, "count"].sum())                  # реакция целиком
        rq = rq[~sm]
        rq["rest"] = (rq["count"] - (rq["fetched"] if full else 0)).clip(lower=0)
        recv_parts, msg_ids = [], set()
        rest = rq[rq["rest"] > 0]
        if len(rest):
            recv_parts.append(rest.groupby(["uid", "key"])["rest"].sum().reset_index().rename(columns={"rest": "n"}))
            msg_ids |= set(rest["message_id"])
            if full:
                r_unattr = int(rest["rest"].sum())
        if full and len(rq):
            uq = _q(db, "SELECT ru.message_id, ru.key, ru.user_id FROM reaction_users ru "
                        "JOIN messages m ON m.message_id = ru.message_id "
                        "WHERE m.guild_id=? AND m.day >= ? AND m.day <= ?", (gid, mid, hi))
            u = uq.merge(rq[["message_id", "key", "uid"]], on=["message_id", "key"], how="inner")
            u = u[~u["user_id"].isin(ign)].copy()
            u["giver"] = _canon(u["user_id"], ident)
            bad = u["user_id"].isin(bots) | (u["giver"] == u["uid"])
            r_self_bot = int(bad.sum())
            g = u[~bad]
            if len(g):
                recv_parts.append(g.groupby(["uid", "key"]).size().reset_index(name="n"))
                r_given = g.groupby(["giver", "key"]).size().reset_index(name="n")
                r_given.columns = REACTION_COLS
                r_given["uid"] = r_given["uid"].astype(str)
                pp = g.groupby(["giver", "uid"]).size().reset_index(name="n")
                pp.columns = REPLY_COLS
                r_pairs = pp.astype({"src": str, "dst": str})
                msg_ids |= set(g["message_id"])
                r_names = {str(x): names.get(x) or str(x) for x in g["giver"].unique()}
        if recv_parts:
            r_recv = pd.concat(recv_parts, ignore_index=True).groupby(["uid", "key"], as_index=False)["n"].sum()
            r_recv["uid"] = r_recv["uid"].astype(str)
        if msg_ids:
            owner = rq.drop_duplicates("message_id").set_index("message_id")["uid"]
            owner = owner[owner.index.isin(msg_ids)]
            r_msgs = pd.DataFrame({"uid": owner.astype(str).values})
            r_msgs = r_msgs.groupby("uid", as_index=False).size()
            r_msgs.columns = REACTION_MSG_COLS
    if spam_filtered:
        notes.append(f"Количество эмодзи и реакций, подвергшихся антиспам-фильтрации: {spam_filtered}.")

    # ---- администрация ----
    staff_rows = []
    if _has_table(db, "staff_snapshot"):
        members: dict = {}
        for r in _q(db, "SELECT uid, display_name, username, kind, roles FROM staff_snapshot WHERE guild_id=?",
                    (gid,)).itertuples():
            u = int(r.uid)
            if u in ident.ignored:
                continue
            c, rl = ident.canon(u), set(json.loads(r.roles or "[]"))
            m = members.get(c)
            if m is None:
                members[c] = {"name": r.display_name or "", "username": r.username or "",
                              "kind": r.kind, "roles": rl}
            else:                                                       # второй аккаунт того же человека
                m["roles"] |= rl
                if r.kind == "owner":
                    m["kind"] = "owner"
        for c, m in members.items():
            name = m["name"] or names.get(c) or str(c)
            label = f"{name} [{m['username']}]" if m["username"] else name
            staff_rows.append((title, str(c), label, m["kind"], ", ".join(sorted(m["roles"])),
                               int(counts.get(c, 0))))
    staff = pd.DataFrame(staff_rows, columns=STAFF_COLS)
    roles_of = _roles_map(staff, False)

    # ---- таблица пользователей ----
    all_ids = set(counts.index) | set(prev["uid"].unique()) | set(address_pairs["src"].astype(int)) \
        | set(address_pairs["dst"].astype(int))
    def shown_name(c: int) -> str:
        nm = names.get(c)
        return nm if nm and nm != str(c) else f"Неизвестный ({c})"

    nameless = [c for c in all_ids if not names.get(c) or names.get(c) == str(c)]
    if nameless:
        notes.append(f"Не удалось определить ник у {len(nameless)} чел. (скорее всего, они ушли с сервера): "
                     "показаны как «Неизвестный (ID)».")
        print(f"  ! Не удалось определить ник у {len(nameless)} чел. ID: "
              + ", ".join(map(str, sorted(nameless))))

    users = pd.DataFrame(
        [(str(c), shown_name(c), unames.get(c, ""), int(counts.get(c, 0)), int(replies.get(c, 0)),
          roles_of.get(str(c), ""), avatars.get(c, "")) for c in sorted(all_ids)], columns=USER_COLS)

    # ---- присутствие, роли, боты ----
    presence, has_presence = _empty(PRESENCE_COLS), False
    snap_ts = _meta(db, f"snapshot_ts:{gid}")
    sync = SyncInfo()
    if snap_ts:
        sync.snapshot_at = datetime.fromtimestamp(int(snap_ts), TZ)
        sync.snapshot_partial = _meta(db, f"snapshot_partial:{gid}") == "1"
        if sync.snapshot_partial:
            warnings.append("Список участников в последнем снимке был неполным: статус «нет на сервере» "
                            "и состав ролей могут быть неточными.")
        if _has_table(db, "presence"):
            pr = _q(db, "SELECT uid, present, joined_ts, first_seen_ts, left_ts FROM presence WHERE guild_id=?", (gid,))
            pr = pr[~pr["uid"].isin(ign)].copy()
            pr["uid"] = _canon(pr["uid"], ident)
            if len(pr):
                g = pr.groupby("uid").agg(present=("present", "max"), joined=("joined_ts", "min"),
                                          first=("first_seen_ts", "min"), left=("left_ts", "max")).reset_index()
                presence = pd.DataFrame({
                    "server": title, "uid": g["uid"].astype(str), "present": g["present"].astype(int),
                    "joined": [_ts_date(v) for v in g["joined"]],
                    "first_seen": [_ts_date(v) for v in g["first"]],
                    "left": [None if p else _ts_date(v) for p, v in zip(g["present"], g["left"])],
                })[PRESENCE_COLS]
                has_presence = True
    else:
        warnings.append("Снимок администрации, ролей и присутствия ещё не сохранён в базе: выполните -sync.")

    member_roles, role_table = _empty(MEMBER_ROLE_COLS), _empty(ROLE_COLS)
    if _has_table(db, "member_roles"):
        rows = [(title, str(int(r.uid)), r.display_name or "", r.username or "", int(r.is_bot),
                 int(r.top_pos), json.loads(r.roles or "[]"))
                for r in _q(db, "SELECT uid, display_name, username, is_bot, top_pos, roles FROM member_roles "
                                "WHERE guild_id=?", (gid,)).itertuples() if int(r.uid) not in ident.ignored]
        if rows:
            member_roles = pd.DataFrame(rows, columns=MEMBER_ROLE_COLS)
    if _has_table(db, "role_snapshot"):
        rows = [(title, str(int(r.role_id)), r.name, int(r.color), int(r.position), int(r.hoist),
                 int(r.mentionable), int(r.managed), int(r.members),
                 frozenset(p for p in (r.perms or "").split(",") if p), int(r.is_default))
                for r in _q(db, "SELECT role_id, name, color, position, hoist, mentionable, managed, perms, "
                                "members, is_default FROM role_snapshot WHERE guild_id=? ORDER BY position DESC",
                            (gid,)).itertuples()]
        if rows:
            role_table = pd.DataFrame(rows, columns=ROLE_COLS)

    # ---- журнал аудита ----
    audit = _load_audit(db, gid, s, e, ident, info, names, person, notes, warnings, sync)

    # ---- состояние синхронизации ----
    incomplete = False
    run = db.execute("SELECT finished_ts, status FROM sync_runs WHERE guild_id=? AND finished_ts IS NOT NULL "
                     "ORDER BY run_id DESC LIMIT 1", (gid,)).fetchone()
    if run:
        sync.last_sync, sync.last_status = datetime.fromtimestamp(run[0], TZ), run[1] or ""
        notes.append("Данные взяты из базы; последняя синхронизация с Discord: "
                     + sync.last_sync.strftime("%d.%m.%Y %H:%M") + ".")
        incomplete = sync.last_status == "cancelled"
        if incomplete:
            warnings.append("Последняя синхронизация была остановлена вручную: данные могут быть неполными.")
        elif sync.last_status == "partial":
            warnings.append("Последняя синхронизация завершилась с ошибками чтения части каналов: "
                            "данные могут быть неполными.")
        if sync.last_sync.date() <= e:
            warnings.append(f"Последняя синхронизация ({sync.last_sync:%d.%m.%Y %H:%M}) была не позже последнего дня "
                            "периода: последние сутки могут быть неполными. Запустите -sync.")
    else:
        warnings.append("В базе нет завершённой синхронизации для этого сервера: данные могут быть пустыми или неполными.")
    g_start = guild_start_date_by_id(gid)
    if s > g_start:
        notes.append(f"Окно отчёта начинается {s}, а история сервера — с {g_start}: «дней из N» и постоянство "
                     "считаются по окну отчёта, а не по всей истории.")

    ds = Dataset(
        path=path, title=title, guild_id=gid, is_merged=False, tz=TZ_NAME,
        period_text="", declared_start=s, declared_end=e,
        data_start=daily["date"].min().date() if len(daily) else None,
        data_end=daily["date"].max().date() if len(daily) else None,
        incomplete=incomplete, compare=compare and len(prev_daily) > 0,
        notes=notes, excluded=excluded,
        users=users, daily=daily, prev_daily=prev_daily, channels=channels, heat=heat, heat_total=heat_total,
        servers=_empty(SERVER_COLS), staff=staff, warnings=warnings,
        reply_pairs=reply_pairs, reply_unresolved=unresolved, reply_self_or_bot=self_or_bot,
        mention_pairs=mention_pairs, address_pairs=address_pairs,
        has_emoji=has_emoji, emoji_user=emoji_user, emoji_info=emoji_info, emoji_days=emoji_days,
        has_reactions=has_rx, reactions_full=full and has_rx, reaction_recv=r_recv, reaction_given=r_given,
        reaction_msgs=r_msgs, reaction_pairs=r_pairs, reaction_names=r_names,
        reaction_self_or_bot=r_self_bot, reaction_unattributed=r_unattr,
        has_audit=audit["mode"] in (1, 3), audit_mode=audit["mode"], audit_users=audit["users"],
        admin_events=audit["events"], audit_actions=audit["actions"], audit_days=audit["days"],
        audit_total=audit["total"], audit_skipped=audit["skipped"], audit_unknown_actor=audit["unknown"],
        has_presence=has_presence, presence=presence, has_roles=len(role_table) > 0,
        member_roles=member_roles, role_table=role_table, channel_parent=channel_parent,
        sync_info={title: sync},
    )
    return ds

def _load_audit(db, gid, s, e, ident, info, names, person, notes, warnings, sync) -> dict:
    out = {"mode": 0, "users": _empty(AUDIT_USER_COLS), "actions": _empty(AUDIT_ACTION_COLS),
           "days": _empty(AUDIT_DAY_COLS, "date"), "events": _empty(ADMIN_EVENT_COLS),
           "total": 0, "skipped": 0, "unknown": 0}
    status = _meta(db, f"audit_status:{gid}")
    if not AUDIT_ENABLED or status not in ("ok", "denied", "failed"):
        return out
    sync.audit_status = status
    sync.audit_truncated = _meta(db, f"audit_truncated:{gid}") == "1"
    since_s = _meta(db, f"audit_since:{gid}")
    sync.audit_since = date.fromisoformat(since_s) if since_s else None
    have_any = db.execute("SELECT 1 FROM audit_entries WHERE guild_id=? LIMIT 1", (gid,)).fetchone() is not None
    mode = {"ok": 1, "denied": 2, "failed": 3}[status]
    if status == "denied" and have_any:
        mode = 3
        notes.append("Журнал аудита: последний сбор не удался (нет права), данные могут быть неполными.")
    out["mode"] = mode
    if mode == 3:
        warnings.append("Журнал аудита собран с ошибкой: показатель «Управление» (G) занижен.")
    if sync.audit_truncated:
        warnings.append("Журнал аудита обрезан лимитом записей: показатель «Управление» (G) неполный.")
    if sync.audit_since and s < sync.audit_since - timedelta(days=AUDIT_RETENTION_DAYS):
        warnings.append(f"Discord хранит журнал аудита около {AUDIT_RETENTION_DAYS} дней, а сбор в базу начался "
                        f"{sync.audit_since:%d.%m.%Y}: записи за начало периода могли быть удалены, "
                        "показатель «Управление» (G) неполный.")
    if mode not in (1, 3):
        return out

    aq = _q(db, "SELECT user_id, action, units, day FROM audit_entries WHERE guild_id=? AND day BETWEEN ? AND ?",
            (gid, s.isoformat(), e.isoformat()))
    out["skipped"] = int(aq["action"].isin(AUDIT_SKIP_ACTIONS).sum())
    aq = aq[~aq["action"].isin(AUDIT_SKIP_ACTIONS)]
    aq = aq[~aq["user_id"].isin(list(ident.ignored))].copy()           # действия игнорируемого не учитываем
    aq["uid"] = _canon(aq["user_id"], ident)
    out["total"] = len(aq)
    out["unknown"] = int((aq["user_id"] == 0).sum())
    if len(aq):
        act = aq.groupby("action").agg(count=("action", "size"), units=("units", "sum")).reset_index()
        out["actions"] = act[AUDIT_ACTION_COLS]
        dy = aq.groupby("day").size().reset_index(name="count")
        dy["date"] = pd.to_datetime(dy["day"])
        out["days"] = dy[AUDIT_DAY_COLS]
        known = aq[aq["user_id"] != 0]
        if len(known):
            anames = {}
            for raw in known["user_id"].unique():
                anames.setdefault(ident.canon(int(raw)), person(int(raw)))
            u = known.groupby(["uid", "action"]).agg(count=("action", "size"), units=("units", "sum")).reset_index()
            u["name"] = [anames.get(int(x)) or names.get(int(x)) or str(x) for x in u["uid"]]
            u["uid"] = u["uid"].astype(str)
            out["users"] = u[AUDIT_USER_COLS]

    if _has_table(db, "audit_events"):
        rows = []
        for r in _q(db, "SELECT day, kind, role_name, subject_id, other_id FROM audit_events "
                        "WHERE guild_id=? AND day BETWEEN ? AND ? ORDER BY ts, entry_id",
                    (gid, s.isoformat(), e.isoformat())).itertuples():
            subj, other = int(r.subject_id), int(r.other_id or 0)
            if subj in ident.ignored:
                continue
            who2 = "-" if (not other or other in ident.ignored) else person(other)
            rows.append((date.fromisoformat(r.day), r.kind, r.role_name or "", person(subj), who2, ""))
        if rows:
            out["events"] = pd.DataFrame(rows, columns=ADMIN_EVENT_COLS)
    return out

# ---------- Суммарный набор ----------

def _merge(items: list, label: str, path: Path) -> Dataset:
    def cat(attr):
        return [getattr(d, attr) for d in items]

    def prefixed(attr):
        return [f"[{d.title}] {x}" for d in items for x in getattr(d, attr)]

    daily = _sum(cat("daily"), ["uid", "date"], DAILY_COLS, "date")
    have_prev = all(d.compare for d in items)
    staff = pd.concat([d.staff for d in items if len(d.staff)], ignore_index=True) \
        if any(len(d.staff) for d in items) else _empty(STAFF_COLS)
    roles = _roles_map(staff, True)

    users = pd.concat([d.users for d in items], ignore_index=True).sort_values("n", ascending=False, kind="stable")
    first = lambda x: next((v for v in x if v), "")
    users = users.groupby("uid", as_index=False, sort=False).agg(
        name=("name", "first"), username=("username", first), n=("n", "sum"),
        replies=("replies", "sum"), avatar=("avatar", first))
    users["role"] = users["uid"].map(roles).fillna("")
    users = users[USER_COLS]

    chans = []
    for d in items:
        c = d.channels.copy()
        if len(c):
            c["channel"] = f"[{d.title}] " + c["channel"]
        chans.append(c)
    channels = pd.concat([c for c in chans if len(c)], ignore_index=True) \
        if any(len(c) for c in chans) else _empty(CHANNEL_COLS)

    srv = []
    for d in items:
        t = d.daily.groupby("uid")["n"].sum().reset_index()
        t["server"] = d.title
        srv.append(t[SERVER_COLS])

    heat = _sum(cat("heat"), ["uid", "weekday", "hour"], HEAT_COLS)
    heat_total = (heat.groupby(["weekday", "hour"], as_index=False)["n"].sum()
                  if len(heat) else _empty(HEAT_TOTAL_COLS))

    einfo = pd.concat([d.emoji_info for d in items if len(d.emoji_info)], ignore_index=True) \
        if any(len(d.emoji_info) for d in items) else _empty(EMOJI_INFO_COLS)
    if len(einfo):
        einfo = einfo.sort_values("external", kind="stable").drop_duplicates("key")[EMOJI_INFO_COLS]

    au = [d.audit_users for d in items if len(d.audit_users)]
    audit_users = (pd.concat(au, ignore_index=True).groupby(["uid", "action"], as_index=False)
                   .agg(name=("name", "first"), count=("count", "sum"), units=("units", "sum"))[AUDIT_USER_COLS]
                   if au else _empty(AUDIT_USER_COLS))
    aa = [d.audit_actions for d in items if len(d.audit_actions)]
    audit_actions = (pd.concat(aa, ignore_index=True).groupby("action", as_index=False)[["count", "units"]].sum()
                     [AUDIT_ACTION_COLS] if aa else _empty(AUDIT_ACTION_COLS))
    ad = [d.audit_days for d in items if len(d.audit_days)]
    audit_days = (pd.concat(ad, ignore_index=True).groupby("date", as_index=False)["count"].sum()[AUDIT_DAY_COLS]
                  if ad else _empty(AUDIT_DAY_COLS, "date"))
    modes = {d.audit_mode for d in items}
    amode = 1 if modes == {1} else 4 if 1 in modes else 3 if 3 in modes else 2 if 2 in modes else 0

    def frames(attr, cols):
        fr = [f for f in cat(attr) if len(f)]
        return pd.concat(fr, ignore_index=True)[cols] if fr else _empty(cols)

    rx_any = [d for d in items if d.has_reactions]
    rnames: dict = {}
    for d in items:
        for k, v in d.reaction_names.items():
            rnames.setdefault(k, v)
    chp: dict = {}
    for d in items:
        chp.update(d.channel_parent)
    sync_info: dict = {}
    for d in items:
        sync_info.update(d.sync_info)
    starts = [d.data_start for d in items if d.data_start]
    ends = [d.data_end for d in items if d.data_end]
    dstart = min(d.declared_start for d in items)

    return Dataset(
        path=path, title=f"Суммарно по серверам: {len(items)}", guild_id=None, is_merged=True, tz=TZ_NAME,
        period_text=_period_text(label, dstart, items[0].declared_end),
        declared_start=dstart, declared_end=items[0].declared_end,
        data_start=min(starts) if starts else None, data_end=max(ends) if ends else None,
        incomplete=any(d.incomplete for d in items), compare=have_prev,
        notes=prefixed("notes"), excluded=prefixed("excluded"),
        users=users, daily=daily,
        prev_daily=_sum(cat("prev_daily"), ["uid", "date"], DAILY_COLS, "date") if have_prev
        else _empty(DAILY_COLS, "date"),
        channels=channels, heat=heat, heat_total=heat_total,
        servers=pd.concat(srv, ignore_index=True)[SERVER_COLS], staff=staff, warnings=prefixed("warnings"),
        reply_pairs=_sum(cat("reply_pairs"), ["src", "dst"], REPLY_COLS),
        reply_unresolved=sum(d.reply_unresolved for d in items),
        reply_self_or_bot=sum(d.reply_self_or_bot for d in items),
        mention_pairs=_sum(cat("mention_pairs"), ["src", "dst"], REPLY_COLS),
        address_pairs=_sum(cat("address_pairs"), ["src", "dst"], REPLY_COLS),
        has_emoji=any(d.has_emoji for d in items),
        emoji_user=_sum(cat("emoji_user"), ["uid", "key", "only"], EMOJI_USER_COLS),
        emoji_info=einfo, emoji_days=_sum(cat("emoji_days"), ["uid", "date"], EMOJI_DAY_COLS, "date"),
        has_reactions=bool(rx_any), reactions_full=bool(rx_any) and all(d.reactions_full for d in rx_any),
        reaction_recv=_sum(cat("reaction_recv"), ["uid", "key"], REACTION_COLS),
        reaction_given=_sum(cat("reaction_given"), ["uid", "key"], REACTION_COLS),
        reaction_msgs=_sum(cat("reaction_msgs"), ["uid"], REACTION_MSG_COLS),
        reaction_pairs=_sum(cat("reaction_pairs"), ["src", "dst"], REPLY_COLS), reaction_names=rnames,
        reaction_self_or_bot=sum(d.reaction_self_or_bot for d in items),
        reaction_unattributed=sum(d.reaction_unattributed for d in items),
        has_audit=amode in (1, 3, 4), audit_mode=amode, audit_users=audit_users,
        admin_events=frames("admin_events", ADMIN_EVENT_COLS),
        audit_actions=audit_actions, audit_days=audit_days,
        audit_total=sum(d.audit_total for d in items),
        audit_skipped=sum(d.audit_skipped for d in items),
        audit_unknown_actor=sum(d.audit_unknown_actor for d in items),
        has_presence=all(d.has_presence for d in items), presence=frames("presence", PRESENCE_COLS),
        has_roles=any(d.has_roles for d in items), member_roles=frames("member_roles", MEMBER_ROLE_COLS),
        role_table=frames("role_table", ROLE_COLS), channel_parent=chp, sync_info=sync_info,
    )

# ---------- Точка входа ----------

def load_db(db_path=None, guild_ids=None, period=None, start=None, end=None,
            compare: bool = False, reactions: str = "auto"):
    """Читает базу бота. Возвращает (список Dataset: серверы, затем суммарный, имя папки для отчётов).
    guild_ids=None — все серверы из базы. Бросает LoaderError."""
    path = Path(db_path).expanduser() if db_path else Path(DB_PATH)
    if not path.exists():
        raise LoaderError(f"База не найдена: {path}")
    try:
        s, e, _key, label = build_period(period, start, end, compare)
    except ValueError as ex:
        raise LoaderError(str(ex))

    db = sqlite3.connect(path)
    db.execute("PRAGMA query_only=ON")
    try:
        tz = _meta(db, "tz")
        if tz and tz != TZ_NAME:
            raise LoaderError(f"База создана с TIMEZONE={tz}, а в config.py задан {TZ_NAME}.")
        in_db = [r[0] for r in db.execute("SELECT DISTINCT guild_id FROM messages ORDER BY guild_id")]
        wanted = in_db if guild_ids is None else list(dict.fromkeys(guild_ids))
        for g in wanted:
            if g not in in_db:
                print(f"⚠ Сервера {g} нет в базе (или в нём нет сообщений) — пропускаю.")
        wanted = [g for g in wanted if g in in_db]

        titles, used = {}, set()
        for g in wanted:
            t = _meta(db, f"guild_name:{g}") or f"Сервер {g}"
            titles[g] = t if t.casefold() not in used else f"{t} ({g})"
            used.add(titles[g].casefold())

        items = []
        for g in wanted:
            g_start = guild_start_date_by_id(g)
            if e < g_start:
                print(f"⚠ «{titles[g]}»: в выбранном периоде у сервера нет истории "
                      f"(начало — {g_start.strftime(DATE_FMT)}) — пропускаю.")
                continue
            pre, gs, cmp = [], max(s, g_start), compare
            if gs != s:
                pre.append(f"История сервера начинается с {g_start.strftime(DATE_FMT)}: период сокращён "
                           f"(запрошено с {s.strftime(DATE_FMT)}).")
            if cmp and gs - timedelta(days=(e - gs).days + 1) < g_start:
                cmp = False
                pre.append("Сравнение с предыдущим периодом отключено: он начался бы раньше "
                           f"начала истории сервера ({g_start.strftime(DATE_FMT)}).")
            print(f"▶ Читаю «{titles[g]}» ({g})…")
            ds = _load_guild(db, path, g, titles[g], gs, e, cmp, reactions, pre)
            ds.period_text = _period_text(label, gs, e)
            items.append(ds)
        if not items:
            raise LoaderError("Нет данных для анализа: ни один сервер не подошёл.")
        out = list(items)
        if len(items) > 1:
            out.append(_merge(items, label, path))
        return out, period_folder_name(s, e, with_year=True)
    finally:
        db.close()