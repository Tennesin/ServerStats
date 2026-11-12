"""Сборка ScanResult из базы с применением игнора, слияния, антиспама и исключённых каналов.
Работает в отдельном потоке: с Discord не связан, использует только ID и простые значения."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import groupby
from types import SimpleNamespace

from config import AUDIT_ENABLED, AUDIT_RETENTION_DAYS, AUDIT_SKIP_ACTIONS, TZ
from db import get_meta, new_connection
from scanner import AdminEvent, ScanResult, ScanState, _reaction_finish, is_spam_dh

def build_result(gid: int, params, state: ScanState, rules, ident, spam_rules) -> ScanResult:
    db = new_connection()
    try:
        return _build(db, gid, params, state, rules, ident, spam_rules)
    finally:
        db.close()

def _excluded_ids(db, gid, rules, state, keep) -> set:
    rows = db.execute("SELECT channel_id, parent_id, name, label FROM channels WHERE guild_id=?",
                      (gid,)).fetchall()
    ex = {cid for cid, _p, name, _l in rows if rules.matches(SimpleNamespace(id=cid, name=name))}
    ex |= {cid for cid, pid, _n, _l in rows if pid in ex}          # ветки исключённого канала
    if keep:
        ex.discard(keep)                                           # явный фильтр сильнее исключений
    known = set(state.excluded)
    for cid, _p, _n, label in rows:
        if cid in ex and label not in known:
            state.excluded.append(label)
            known.add(label)
    return ex

def _build(db, gid, params, state, rules, ident, spam_rules) -> ScanResult:
    first = params.prev_range[0] if params.prev_range else params.start_date
    lo = first.isoformat() if first else "0000-01-01"
    hi = params.end_date.isoformat() if params.end_date else "9999-12-31"

    only_chan = params.channel.id if params.channel else None
    excluded = _excluded_ids(db, gid, rules, state, only_chan)
    allowed = None
    if only_chan:
        allowed = {only_chan} | {cid for (cid,) in db.execute(
            "SELECT channel_id FROM channels WHERE guild_id=? AND parent_id=?", (gid, only_chan))}
    user_id = ident.canon(params.user.id) if params.user else None

    # Режим реакций: «полный» без списков людей в базе превращается в «счётчики»
    rx_mode = params.reactions
    if rx_mode == "full" and not db.execute("SELECT 1 FROM reaction_users LIMIT 1").fetchone():
        rx_mode = "counts"
        state.notes.append(
            "В базе нет списков людей, ставивших реакции (они собираются только при синхронизации "
            "в режиме «полный»): реакции показаны как «счётчики».")
    eff = replace(params, reactions=rx_mode)

    main = ScanResult()
    main.reaction_mode = rx_mode
    prev = None
    if params.prev_range:
        prev = ScanResult()
        prev.reaction_mode = rx_mode
        prev.names, prev.usernames, prev.channel_names = main.names, main.usernames, main.channel_names
        main.prev = prev

    info = {r[0]: r[1:] for r in db.execute(
        "SELECT uid, display_name, username, avatar_url, is_bot, last_seen_ts FROM users WHERE guild_id=?",
        (gid,))}
    bots = {u for u, v in info.items() if v[3]}

    day_cache: dict = {}

    def to_day(s):
        d = day_cache.get(s)
        if d is None:
            d = day_cache[s] = date.fromisoformat(s)
        return d

    def route(author_id, channel_id, day_s):
        """(uid, target, date) или None, если запись надо пропустить."""
        if channel_id in excluded or (allowed is not None and channel_id not in allowed):
            return None
        if ident.skip(author_id):
            return None
        uid = ident.canon(author_id)
        if user_id and uid != user_id:
            return None
        day = to_day(day_s)
        target = prev if (prev is not None and day < params.start_date) else main
        return uid, target, day

    # ---- сообщения и ответы ----
    q = ("SELECT message_id, author_id, channel_id, day, hour, weekday, is_reply, reply_to, reply_state "
         "FROM messages WHERE guild_id=? AND day BETWEEN ? AND ?")
    for _mid, author, cid, day_s, hour, wd, is_reply, reply_to, rstate in db.execute(q, (gid, lo, hi)):
        r = route(author, cid, day_s)
        if r is None:
            continue
        uid, target, day = r
        target.user_day[(uid, day)] += 1
        target.user_channel[(uid, cid)] += 1
        target.heat[(wd, hour)] += 1
        target.user_heat[(uid, wd, hour)] += 1
        if target is main:
            state.counted += 1
        if is_reply:
            target.replies[uid] += 1
            if rstate == 1:
                dst_id = ident.canon(reply_to)
                if ident.skip(reply_to):
                    pass                                   # ответ игнорируемому: не учитываем
                elif reply_to in bots or dst_id == uid:
                    target.reply_self_or_bot += 1
                else:
                    target.reply_pairs[(uid, dst_id)] += 1
            else:
                target.reply_unresolved += 1

    # ---- теги ----
    q = ("SELECT m.author_id, m.channel_id, m.day, m.reply_to, t.target_id FROM mentions t "
         "JOIN messages m ON m.message_id = t.message_id "
         "WHERE m.guild_id=? AND m.day BETWEEN ? AND ?")
    for author, cid, day_s, reply_to, tgt in db.execute(q, (gid, lo, hi)):
        r = route(author, cid, day_s)
        if r is None:
            continue
        uid, target, _d = r
        t_id = ident.canon(tgt)
        reply_dst = ident.canon(reply_to) if reply_to else None
        if tgt in bots or ident.skip(tgt) or t_id == uid or t_id == reply_dst:
            continue
        target.mention_pairs[(uid, t_id)] += 1

    # ---- эмодзи в тексте ----
    q = ("SELECT m.author_id, m.channel_id, m.day, m.hour, m.message_id, e.key, e.only FROM emoji e "
         "JOIN messages m ON m.message_id = e.message_id "
         "WHERE m.guild_id=? AND m.day BETWEEN ? AND ?")
    only_done = set()
    for author, cid, day_s, hour, mid, key, only in db.execute(q, (gid, lo, hi)):
        r = route(author, cid, day_s)
        if r is None:
            continue
        uid, target, day = r
        if is_spam_dh(spam_rules, key, day, hour):
            state.spam_filtered += 1
            continue
        target.emoji_user[(uid, key, only)] += 1
        if only and mid not in only_done:                  # «только эмодзи»: если что-то осталось после фильтра
            only_done.add(mid)
            target.emoji_only_day[(uid, day)] += 1
    for key, name, anim, ext in db.execute(
            "SELECT key, name, animated, external FROM emoji_info WHERE guild_id=?", (gid,)):
        main.emoji_info[key] = (name or "", anim, ext)

    # ---- реакции ----
    if rx_mode != "off":
        _load_reactions(db, gid, lo, hi, eff, main, route, ident, bots, spam_rules, state)
        state.reaction_msgs = sum(main.reaction_msgs.values())
        _reaction_finish(main, state, eff)

    # ---- ники, аватарки, названия каналов ----
    name_ts: dict = {}
    for uid, (disp, uname, avatar, is_bot, last_ts) in info.items():
        if is_bot or ident.skip(uid):
            continue
        c = ident.canon(uid)
        if c not in name_ts or last_ts >= name_ts[c]:      # показываем аккаунт, писавший позже
            name_ts[c] = last_ts
            main.names[c] = disp or str(c)
            main.usernames[c] = uname or ""
            main.avatars[c] = avatar or ""
    chan_rows = db.execute("SELECT channel_id, label FROM channels WHERE guild_id=?", (gid,)).fetchall()
    for cid, label in chan_rows:
        main.channel_names[cid] = label
    if not state.total_channels:                           # режим без синхронизации
        state.total_channels = state.channels_done = sum(1 for cid, _l in chan_rows if cid not in excluded)

    state.text_seen = db.execute(
        "SELECT COUNT(*) FROM messages WHERE guild_id=? AND day BETWEEN ? AND ? AND has_text=1",
        (gid, lo, hi)).fetchone()[0]

    # ---- справка об источнике ----
    row = db.execute("SELECT MAX(finished_ts) FROM sync_runs WHERE guild_id=? AND status IN ('ok','partial')",
                     (gid,)).fetchone()
    if row and row[0]:
        state.notes.append("Данные взяты из базы; последняя полная синхронизация с Discord: "
                           + datetime.fromtimestamp(row[0], TZ).strftime("%d.%m.%Y %H:%M") + ".")
    else:
        state.notes.append("В базе нет завершённой синхронизации для этого сервера: данные могут быть "
                           "пустыми или неполными. Запустите сбор без --nosync.")

    _load_audit(db, gid, params, main, ident, user_id, info, state, to_day)
    return main

def _load_reactions(db, gid, lo, hi, params, main, route, ident, bots, spam_rules, state):
    full = params.reactions == "full"
    q = ("SELECT m.author_id, m.channel_id, m.day, m.hour, r.message_id, r.key, r.count, r.fetched, ru.user_id "
         "FROM reactions r JOIN messages m ON m.message_id = r.message_id "
         "LEFT JOIN reaction_users ru ON ru.message_id = r.message_id AND ru.key = r.key "
         "WHERE m.guild_id=? AND m.day BETWEEN ? AND ? ORDER BY r.message_id, r.key")
    msg_done = set()
    for (mid, key), grp in groupby(db.execute(q, (gid, lo, hi)), key=lambda x: (x[4], x[5])):
        grp = list(grp)
        author, cid, day_s, hour, _m, _k, count, fetched, _u = grp[0]
        r = route(author, cid, day_s)
        if r is None:
            continue
        uid, target, day = r
        if is_spam_dh(spam_rules, key, day, hour):
            state.spam_filtered += count                  # вся реакция целиком
            continue
        got = False
        fetched = fetched if full else 0
        if fetched:
            for row in grp:
                u = row[8]
                if u is None or ident.skip(u):
                    continue
                giver = ident.canon(u)
                if u in bots or giver == uid:
                    target.reaction_self_or_bot += 1
                    continue
                target.reaction_recv[(uid, key)] += 1
                target.reaction_given[(giver, key)] += 1
                target.reaction_pairs[(giver, uid)] += 1
                got = True
        rest = max(0, count - fetched)
        if rest:                                          # авторы неизвестны: «получено» без фильтра
            target.reaction_recv[(uid, key)] += rest
            if full:
                target.reaction_unattributed += rest
            got = True
        if got and mid not in msg_done:
            msg_done.add(mid)
            target.reaction_msgs[uid] += 1

def _load_audit(db, gid, params, main, ident, user_id, info, state, to_day):
    main.audit_mode = "off"
    if not AUDIT_ENABLED:
        return
    if params.channel is not None:
        state.notes.append("Журнал аудита не использовался: задан фильтр по каналу (в журнале нет привязки к каналам).")
        return
    status = get_meta(db, f"audit_status:{gid}")
    if status not in ("ok", "denied", "failed"):
        return                                            # аудит ни разу не собирался

    have_any = db.execute("SELECT 1 FROM audit_entries WHERE guild_id=? LIMIT 1", (gid,)).fetchone()
    mode = status
    if status == "denied" and have_any:
        mode = "failed"                                   # данные есть, но последний сбор не удался
        state.notes.append("Журнал аудита: последний сбор не удался (нет права), данные могут быть неполными.")
    main.audit_mode = mode

    if get_meta(db, f"audit_truncated:{gid}") == "1":
        main.audit_truncated = 1
        state.notes.append("Журнал аудита обрезан лимитом записей: данные неполные (следующий сбор продолжит с места остановки).")
    since_s = get_meta(db, f"audit_since:{gid}")
    start = params.start_date
    if since_s:
        since = date.fromisoformat(since_s)
        if start is None or start < since - timedelta(days=AUDIT_RETENTION_DAYS):
            state.notes.append(
                f"Журнал аудита хранится Discord около {AUDIT_RETENTION_DAYS} дней, а сбор в базу начался "
                f"{since.strftime('%d.%m.%Y')}: записи за начало периода могли быть удалены, данные аудита неполные.")
    if params.user:
        state.notes.append("Журнал аудита отфильтрован по автору действия (выбран пользователь).")

    a_lo = start.isoformat() if start else "0000-01-01"
    a_hi = params.end_date.isoformat() if params.end_date else "9999-12-31"
    q = "SELECT user_id, action, units, day FROM audit_entries WHERE guild_id=? AND day BETWEEN ? AND ?"
    for raw, code, units, day_s in db.execute(q, (gid, a_lo, a_hi)):
        if code in AUDIT_SKIP_ACTIONS:
            main.audit_skipped += 1
            continue
        if raw:
            if ident.skip(raw):
                continue                                  # действия игнорируемого не учитываем
            uid = ident.canon(raw)
            if user_id and uid != user_id:
                continue
            nm = info.get(raw)
            if nm and nm[0]:
                main.audit_names[uid] = nm[0]
        else:
            if user_id:
                continue
            uid = 0
            main.audit_unknown_actor += 1
        day = to_day(day_s)
        main.audit_total += 1
        main.audit_actions[code] += 1
        main.audit_units[code] += units
        main.audit_user_action[(uid, code)] += 1
        main.audit_user_units[(uid, code)] += units
        main.audit_day[day] += 1
        if main.audit_first is None or day < main.audit_first:
            main.audit_first = day
        if main.audit_last is None or day > main.audit_last:
            main.audit_last = day

    # ---- ключевые события администрации ----
    ev_q = ("SELECT day, kind, role_name, subject_id, other_id FROM audit_events "
            "WHERE guild_id=? AND day BETWEEN ? AND ? ORDER BY ts, entry_id")
    for day_s, kind, role, subj, other in db.execute(ev_q, (gid, a_lo, a_hi)):
        if ident.skip(subj):
            continue
        a = ident.canon(subj)
        b = 0 if (not other or ident.skip(other)) else ident.canon(other)
        if user_id and user_id not in (a, b):
            continue
        main.admin_events.append(AdminEvent(day=to_day(day_s), kind=kind, role=role, uid=a, uid2=b))
        for raw, c in ((subj, a), (other, b)):
            if c and raw in info and info[raw][0]:
                main.event_names.setdefault(c, info[raw][0])