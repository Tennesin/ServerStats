"""Этап 2: метрики сервера и пользователей. Работает только с Dataset, без Discord и сети."""
import math
import numpy as np
import pandas as pd
from loader import Dataset

from emoji_tools import info_map, label_text

LEVELS = ["низкая", "средняя", "высокая"]
NIGHT_HOURS = 6            # ночь: часы 0..5
DORMANT_DAYS = 7           # «притих»: нет сообщений последние N дней окна
MIN_TREND_DAYS = 14        # меньше — тренды не считаем
MIN_USER_MSGS_TREND = 20   # меньше сообщений — тренд пользователя не оцениваем
TREND_UP = 1.3
TREND_DOWN = 0.7
MIN_USER_ACTIVE_DAYS_TREND = 5   # меньше активных дней — тренд пользователя не оцениваем
BURST_TOP3_SHARE = 0.6           # «пачками»: 3 самых активных дня дают ≥ 60% сообщений
TREND_WINDOW = 7                 # «недавняя» динамика сервера: последние N дней к предыдущим N
PEAK_RATIO = 1.5                 # самая активная неделя выше первой недели в ≥ N раз
DECAY_RATIO = 0.6                # и последняя неделя ≤ N × пик -> «всплеск и спад»
HEAVY_WEEK_FACTOR = 1.6          # неделя «перетягивает» окно, если её доля ≥ N × (7 / длина окна)
CHANGE_EPS = 0.15                # изменение меньше ±15% считается «стабильно»
CHANGE_MIXED = 0.30              # противоположные сигналы слабее ±30% не считаются противоречием

# ---------- Вспомогательное ----------

def _lvl(value, mid, high) -> int:
    return 2 if value >= high else 1 if value >= mid else 0

def make_rel(level: int, notes: list) -> dict:
    level = max(0, min(2, int(level)))
    return {"level": LEVELS[level], "notes": notes}

def _cap(rel: dict, cap_level: int) -> dict:
    """Не даёт достоверности раздела превысить общий уровень."""
    return make_rel(min(LEVELS.index(rel["level"]), cap_level), rel["notes"])

def _sign(x, th: float = CHANGE_EPS) -> int:
    return 1 if x > th else -1 if x < -th else 0

def calendar_days(ds: Dataset) -> pd.DatetimeIndex:
    """Все календарные дни окна анализа (заявленный период, расширенный фактическими данными)."""
    a, b = ds.window
    if ds.data_start:
        a = min(a, ds.data_start) if a else ds.data_start
    if ds.data_end:
        b = max(b, ds.data_end) if b else ds.data_end
    if not a or not b:
        return pd.DatetimeIndex([])
    return pd.date_range(a, b, freq="D")

def _ok(v) -> bool:
    """Значение есть (не None и не NaN/NaT)."""
    return v is not None and not pd.isna(v)

def grouped_channels(ds: Dataset) -> pd.DataFrame:
    """ds.channels + колонки group_id / group_label / is_thread.
    Ветка сворачивается в родительский канал (нужен ds.channel_parent, он есть у источника «БД»).
    У xlsx-источника ветки не различаются: каждый канал сам себе группа."""
    ch = ds.channels
    if ch is None or ch.empty:
        return ch
    ch = ch.copy()
    parent = ds.channel_parent or {}
    labels = dict(zip(ch["channel_id"], ch["channel"]))
    gid, glabel, thread = [], [], []
    for cid, label in zip(ch["channel_id"], ch["channel"]):
        pid = parent.get(cid)
        if pid is None:
            gid.append(cid)
            glabel.append(label)
            thread.append(False)
            continue
        pid = str(pid)
        gid.append(pid)
        glabel.append(labels.get(pid) or label.split(" › ")[0])   # родитель без сообщений: имя берём из подписи ветки
        thread.append(True)
    ch["group_id"], ch["group_label"], ch["is_thread"] = gid, glabel, thread
    return ch

def _presence_block(ds: Dataset, writers: set, dormant: set, per_user: pd.Series, days):
    """Сколько писавших уже нет на сервере и кто вступил в окне. None — снимка присутствия нет."""
    present = ds.present_ids()
    if present is None:
        return None
    p = ds.presence
    a, b = days[0].date(), days[-1].date()
    partial = any(si.snapshot_partial for si in ds.sync_info.values())
    snaps = [si.snapshot_at for si in ds.sync_info.values() if si.snapshot_at]

    if partial:                                   # список участников был неполным: «ушедший» только если бот видел его уход
        absent = set(p.loc[p["present"] == 0, "uid"])
        gone = (writers & absent) - present
    else:
        gone = writers - present
    total = float(per_user.sum())
    gone_msgs = float(per_user.reindex(list(gone)).fillna(0).sum()) if gone else 0.0

    joined_uids = {r.uid for r in p.itertuples(index=False) if _ok(r.joined) and a <= r.joined <= b}
    left_rows = sum(1 for r in p.itertuples(index=False)
                    if r.present == 0 and _ok(r.left) and a <= r.left <= b)

    notes = ["«нет на сервере» — человека не было в списке участников в момент последнего снимка; "
             "дата ухода приблизительная (когда бот это заметил)",
             "дата вступления — по данным Discord (при повторном входе обновляется)"]
    if partial:
        notes.append("список участников в снимке был неполным: «ушедших» может быть больше, чем показано")
    if snaps:
        notes.append(f"снимок присутствия сделан {min(snaps):%d.%m.%Y %H:%M}")
    return {
        "writers": len(writers), "gone": len(gone),
        "gone_share": len(gone) / len(writers) if writers else 0.0,
        "gone_msg_share": gone_msgs / total if total else 0.0,
        "dormant_present": len(dormant & present), "dormant_gone": len(dormant & gone),
        "joined": len(joined_uids), "joined_wrote": len(joined_uids & writers),
        "left": left_rows, "notes": notes,
    }

def gini(values) -> float:
    x = np.sort(np.asarray(values, dtype=float))
    n = len(x)
    if n == 0 or x.sum() == 0:
        return 0.0
    cum = np.cumsum(x)
    return float((n + 1 - 2 * cum.sum() / cum[-1]) / n)

def _top_share(desc: np.ndarray, k: int, total: float) -> float:
    return float(desc[:k].sum() / total) if total else 0.0

def base_reliability(ds: Dataset, days_n: int, users_n: int) -> dict:
    notes, lv = [], min(_lvl(days_n, 14, 28), _lvl(ds.total_messages, 300, 2000), _lvl(users_n, 10, 30))
    if days_n < 14:
        notes.append(f"короткое окно ({days_n} дн.): тренды и сезонность ненадёжны")
    if ds.total_messages < 300:
        notes.append("мало сообщений: доли и распределения шумные")
    if users_n < 10:
        notes.append("мало пользователей: распределения и сегменты неустойчивы")
    if ds.incomplete:
        lv -= 1
        notes.append("сбор остановлен вручную: данные неполные")
    if ds.truncated:
        lv -= 1
        notes.append("данные по дням обрезаны лимитом строк")
    if getattr(ds, "filtered", ""):
        lv = 0
        notes.append("отчёт с фильтром, это не весь сервер: " + ds.filtered)
    return make_rel(lv, notes)


# ---------- Блоки метрик сервера ----------

def _trend_block(tot: pd.Series, D: int) -> dict:
    if D < MIN_TREND_DAYS:
        return {"available": False,
                "rel": make_rel(0, [f"окно {D} дн. < {MIN_TREND_DAYS}: тренд не оценивается"])}
    v = tot.values.astype(float)
    total = float(v.sum())
    w = TREND_WINDOW
    h = D // 2
    first, second = float(v[:h].mean()), float(v[h:].mean())
    half_change = (second / first - 1) if first > 0 else None

    last_avg, prev_avg = float(v[-w:].mean()), float(v[-2 * w:-w].mean())
    recent_change = (last_avg / prev_avg - 1) if prev_avg > 0 else None
    first_week_avg = float(v[:w].mean())

    # самая активная неделя (скользящее окно в w дней)
    roll = np.convolve(v, np.ones(w), mode="valid")
    k = int(roll.argmax())
    peak = {"start": tot.index[k].date(), "end": tot.index[k + w - 1].date(),
            "avg": float(roll[k] / w), "share": float(roll[k] / total) if total else 0.0}
    peak_passed = (peak["avg"] >= PEAK_RATIO * max(first_week_avg, 1e-9)
                   and last_avg <= DECAY_RATIO * peak["avg"])
    heavy = D >= 3 * w and peak["share"] >= HEAVY_WEEK_FACTOR * w / D

    if peak_passed:
        direction = "всплеск и спад"
    elif recent_change is None or half_change is None:
        direction = "н/д"
    else:
        a, b = _sign(recent_change), _sign(half_change)
        if a * b == -1 and max(abs(recent_change), abs(half_change)) >= CHANGE_MIXED:
            direction = "неоднозначно"          # сигналы противоположны
        else:
            direction = {1: "рост", -1: "спад", 0: "стабильно"}[b]

    wk = tot.groupby(tot.index.to_period("W")).agg(["sum", "count"])
    wk.index = [p.start_time.date() for p in wk.index]
    wk.columns = ["messages", "days"]
    wd = tot.groupby(tot.index.weekday).mean().reindex(range(7)).fillna(0.0)

    notes = ["неполные недели по краям окна не сопоставимы с полными (см. колонку «Дней»)"]
    level = _lvl(D, 21, 42)
    if D < 42:
        notes.append(f"окно {D} дн. ({D // w} полных недель): для устойчивого тренда нужно хотя бы 6 недель")
    if heavy or direction in ("всплеск и спад", "неоднозначно"):
        level = min(level, 1)
        notes.append(f"одна неделя дала {peak['share'] * 100:.0f}% сообщений окна: "
                     "направление определяется всплеском")
    return {
        "available": True, "direction": direction, "change": half_change,
        "first_half_avg": first, "second_half_avg": second,
        "window": w, "last_avg": last_avg, "prev_avg": prev_avg, "recent_change": recent_change,
        "first_week_avg": first_week_avg, "peak": peak, "heavy": heavy,
        "weekly": wk, "weekday_avg": [float(x) for x in wd.values],
        "rel": make_rel(level, notes),
    }

def _time_block(ds: Dataset):
    ht = ds.heat_total
    if ht is None or ht.empty or ht["n"].sum() == 0:
        return None
    m = np.zeros((7, 24))
    for r in ht.itertuples(index=False):
        if 0 <= r.weekday < 7 and 0 <= r.hour < 24:
            m[r.weekday, r.hour] += r.n
    total = m.sum()
    if total == 0:
        return None
    hour_tot, wd_tot = m.sum(axis=0), m.sum(axis=1)
    sums = [sum(hour_tot[(h + k) % 24] for k in range(3)) for h in range(24)]
    start = int(np.argmax(sums))
    return {
        "matrix": m, "peak_hour": int(hour_tot.argmax()), "peak_weekday": int(wd_tot.argmax()),
        "night_share": float(hour_tot[:NIGHT_HOURS].sum() / total),
        "weekend_share": float(wd_tot[5:].sum() / total),
        "best3_start": start, "best3_share": float(sums[start] / total),
        "rel": make_rel(_lvl(total, 300, 2000), ["часовой пояс: " + (ds.tz or "не указан")]),
    }

def _segments(active_days: pd.Series, per_user: pd.Series, D: int) -> pd.DataFrame:
    ad = active_days.reindex(per_user.index).values
    share = ad / D
    seg = np.select(
        [ad == 1, share >= 0.5, share >= 0.2],
        ["разовые", "ядро", "регулярные"], default="эпизодические")
    total = float(per_user.sum())
    rows = []
    for name in ["ядро", "регулярные", "эпизодические", "разовые"]:
        mask = seg == name
        msgs = float(per_user.values[mask].sum())
        rows.append((name, int(mask.sum()), mask.sum() / len(per_user),
                     int(msgs), msgs / total if total else 0.0))
    return pd.DataFrame(rows, columns=["segment", "users", "user_share", "messages", "message_share"]
                        ).set_index("segment")

def server_metrics(ds: Dataset) -> dict:
    days = calendar_days(ds)
    D = len(days)
    daily = ds.daily
    if daily.empty or D == 0:
        return {"empty": True, "reliability": make_rel(0, ["нет сообщений за период"])}

    total_msgs = int(daily["n"].sum())
    per_user = daily.groupby("uid")["n"].sum()
    U = len(per_user)
    g = daily.groupby("uid")["date"].agg(["min", "max", "nunique"])

    out = {"empty": False, "days": D, "messages": total_msgs, "users": U,
           "reliability": base_reliability(ds, D, U)}

    # Ряды по дням
    tot = daily.groupby("date")["n"].sum().reindex(days, fill_value=0)
    dau = daily.groupby("date")["uid"].nunique().reindex(days, fill_value=0)
    out["series"] = pd.DataFrame({"messages": tot, "dau": dau})

    replies_total = int(ds.users["replies"].sum()) if ds.full_data and len(ds.users) else None
    out["overview"] = {
        "active_days": int((tot > 0).sum()),
        "avg_per_day": total_msgs / D,
        "median_per_day": float(tot.median()),
        "peak_day": tot.idxmax().date(), "peak_messages": int(tot.max()),
        "avg_dau": float(dau.mean()), "max_dau": int(dau.max()),
        "stickiness": float(dau.mean() / U),
        "msgs_per_user_mean": total_msgs / U,
        "msgs_per_user_median": float(per_user.median()),
        "reply_share": (replies_total / total_msgs) if replies_total is not None else None,
    }
    out["trend"] = _trend_block(tot, D)

    # Распределение
    vals = np.sort(per_user.values.astype(float))[::-1]
    total = float(vals.sum())
    cum = np.cumsum(vals)
    k10 = max(1, math.ceil(0.1 * U))
    out["distribution"] = {
        "gini": gini(vals), "top1": _top_share(vals, 1, total), "top5": _top_share(vals, 5, total),
        "top10pct": _top_share(vals, k10, total), "k10": k10,
        "users_for_50": int(np.searchsorted(cum, 0.5 * total, side="left") + 1),
        "users_for_80": int(np.searchsorted(cum, 0.8 * total, side="left") + 1),
        "mean": float(vals.mean()), "median": float(np.median(vals)),
        "p90": float(np.percentile(vals, 90)), "max": int(vals[0]),
        "rel": make_rel(_lvl(U, 10, 30), []),
    }
    out["segments"] = _segments(g["nunique"], per_user, D)
    out["time"] = _time_block(ds)

    # Жизненный цикл
    first_seen = g["min"]
    from_start = int((first_seen == days[0]).sum())       # писали уже в первый день окна
    later = first_seen[first_seen > days[0]]
    nbw = later.dt.to_period("W").value_counts().sort_index()
    life = {
        "from_start": from_start,
        "new_by_week": {p.start_time.date(): int(v) for p, v in nbw.items()},
    }
    if D >= MIN_TREND_DAYS:
        half = days[D // 2]
        early = set(daily.loc[daily["date"] < half, "uid"])
        late = set(daily.loc[daily["date"] >= half, "uid"])
        life.update(
            retained=len(early & late), lost=len(early - late), new_second_half=len(late - early),
            retention=(len(early & late) / len(early)) if early else None)
    since = (days[-1] - g["max"]).dt.days
    dormant = int((since >= DORMANT_DAYS).sum())
    life.update(dormant=dormant, dormant_share=dormant / U)
    life["presence"] = _presence_block(ds, set(per_user.index), set(since.index[since >= DORMANT_DAYS]),
                                       per_user, days)
    life_notes = [
        "«первое появление» = первое сообщение в окне, а не вступление на сервер",
        f"«притих» = нет сообщений последние {DORMANT_DAYS} дн. окна"]
    if life["presence"]:
        life_notes += life["presence"]["notes"]
    life["rel"] = make_rel(_lvl(D, 14, 28), life_notes)
    out["lifecycle"] = life

    # Сравнение с прошлым периодом
    out["compare"] = None
    if ds.compare and not ds.prev_daily.empty:
        prev_tot = ds.prev_daily.groupby("uid")["n"].sum()
        cur_u, prev_u = set(per_user.index), set(prev_tot.index)
        both = cur_u & prev_u
        pm = int(prev_tot.sum())
        out["compare"] = {
            "prev_messages": pm, "messages_change": (total_msgs / pm - 1) if pm else None,
            "prev_users": len(prev_u), "users_change": (U / len(prev_u) - 1) if prev_u else None,
            "retained": len(both), "new": len(cur_u - prev_u), "lost": len(prev_u - cur_u),
            "retention": (len(both) / len(prev_u)) if prev_u else None,
            "rel": make_rel(_lvl(D, 14, 28), ["«новый»/«ушёл» = писал/не писал, а не вступил/покинул сервер"]),
        }

    # Каналы (ветки считаются вместе с родительским каналом)
    out["channels"] = None
    ch = grouped_channels(ds)
    if ch is not None and not ch.empty:
        t = (ch.groupby("group_id")
               .agg(channel=("group_label", "first"), messages=("n", "sum"), authors=("uid", "nunique"))
               .sort_values("messages", ascending=False))
        thr = (ch[ch["is_thread"]].groupby("group_id")
                 .agg(thread_messages=("n", "sum"), threads=("channel_id", "nunique")))
        t["thread_messages"] = thr["thread_messages"].reindex(t.index).fillna(0).astype(int)
        t["threads"] = thr["threads"].reindex(t.index).fillna(0).astype(int)
        t["share"] = t["messages"] / t["messages"].sum()
        k_user = ch.groupby("uid")["group_id"].nunique()
        out["channels"] = {
            "table": t, "top3_share": float(t["share"].values[:3].sum()),
            "hhi": float((t["share"].values ** 2).sum()),
            "single_channel_share": float((k_user == 1).mean()),
            "mean_channels": float(k_user.mean()),
            "thread_share": float(t["thread_messages"].sum() / t["messages"].sum()),
            "thread_count": int(ch.loc[ch["is_thread"], "channel_id"].nunique()),
            "rel": make_rel(_lvl(total_msgs, 300, 2000), []),
        }

    # Серверы (суммарный файл)
    out["servers"] = None
    if not ds.servers.empty:
        sv = ds.servers
        t = (sv.groupby("server").agg(messages=("n", "sum"), authors=("uid", "nunique"))
               .sort_values("messages", ascending=False))
        t["share"] = t["messages"] / t["messages"].sum()
        multi = int((sv.groupby("uid")["server"].nunique() > 1).sum())
        out["servers"] = {"table": t, "multi_server_users": multi,
                          "multi_server_share": multi / sv["uid"].nunique()}

    # Администрация
    out["staff"] = None
    if not ds.staff.empty:
        sm = int(ds.staff["n"].sum())
        out["staff"] = {
            "people": int(ds.staff["uid"].nunique()), "messages": sm,
            "share": sm / total_msgs if total_msgs else 0.0,
            "by_kind": {k: int(v) for k, v in ds.staff.groupby("kind")["n"].sum().items()},
        }
    # Достоверность раздела не выше общей (иначе шапка говорит «средняя», а разделы — «высокая»)
    cap = LEVELS.index(out["reliability"]["level"])
    for key in ("trend", "distribution", "time", "lifecycle", "compare", "channels"):
        blk = out.get(key)
        if blk and "rel" in blk:
            blk["rel"] = _cap(blk["rel"], cap)
    return out

# ---------- Личные метрики ----------

def reply_metrics(ds: Dataset, uids) -> pd.DataFrame:
    """Метрики общения по обращениям (ответы + теги). Если данных нет (старый файл) — всё NaN."""
    num_cols = ["replies_in", "reply_sources", "replies_out_known", "reply_targets",
                "in_weighted", "reciprocity", "top_target_share", "top_source_share"]
    out = pd.DataFrame(index=uids)
    for c in num_cols:
        out[c] = np.nan
    out["top_target"] = ""
    out["top_source"] = ""

    rp = ds.address_pairs
    if rp is None or rp.empty:
        return out
    rp = rp.copy()
    rp["sq"] = np.sqrt(rp["n"])

    inc = rp.groupby("dst").agg(replies_in=("n", "sum"), reply_sources=("src", "nunique"),
                                in_weighted=("sq", "sum"))
    outg = rp.groupby("src").agg(replies_out_known=("n", "sum"), reply_targets=("dst", "nunique"))

    for c in ("replies_in", "reply_sources", "in_weighted"):
        out[c] = inc[c].reindex(uids).fillna(0).values
    for c in ("replies_out_known", "reply_targets"):
        out[c] = outg[c].reindex(uids).fillna(0).values

    # Взаимность: какая доля обращений человека ушла тем, кто хоть раз обратился к нему
    back = rp[["src", "dst", "n"]].rename(columns={"src": "dst", "dst": "src", "n": "n_back"})
    mm = rp.merge(back, on=["src", "dst"], how="left")
    mm["mutual"] = mm["n"].where(mm["n_back"].fillna(0) > 0, 0)
    rec = mm.groupby("src")["mutual"].sum() / mm.groupby("src")["n"].sum()
    out["reciprocity"] = rec.reindex(uids).values

    # Главный адресат (к кому он обращается чаще всего)
    t = (rp.sort_values(["src", "n"], ascending=[True, False])
           .drop_duplicates("src").set_index("src"))
    out["top_target"] = t["dst"].reindex(uids).fillna("").values
    out["top_target_share"] = (t["n"] / outg["replies_out_known"]).reindex(uids).values

    # Главный «обращающийся» (кто чаще всего обращается к нему)
    s = (rp.sort_values(["dst", "n"], ascending=[True, False])
           .drop_duplicates("dst").set_index("dst"))
    out["top_source"] = s["src"].reindex(uids).fillna("").values
    out["top_source_share"] = (s["n"] / inc["replies_in"]).reindex(uids).values
    return out

def user_metrics(ds: Dataset) -> pd.DataFrame:
    days = calendar_days(ds)
    D = len(days)
    daily = ds.daily
    if daily.empty or D == 0:
        return pd.DataFrame()

    pivot = (daily.pivot_table(index="uid", columns="date", values="n", aggfunc="sum", fill_value=0)
                  .reindex(columns=days, fill_value=0))
    vals = pivot.values.astype(np.int64)
    act = vals > 0
    uids = pivot.index
    U = len(uids)

    df = pd.DataFrame(index=uids)
    df.index.name = "uid"
    n = vals.sum(axis=1)
    ad = act.sum(axis=1)
    df["messages"] = n
    df["active_days"] = ad
    df["active_share"] = ad / D
    df["avg_per_active_day"] = n / np.maximum(ad, 1)
    df["max_day"] = vals.max(axis=1)

    first = np.argmax(act, axis=1)
    last = D - 1 - np.argmax(act[:, ::-1], axis=1)
    df["first_date"] = days[first].date
    df["last_date"] = days[last].date
    df["days_since_last"] = (D - 1) - last

    # Серии активных дней подряд
    cur = np.zeros(U, dtype=int)
    longest = np.zeros(U, dtype=int)
    for j in range(D):
        cur = np.where(act[:, j], cur + 1, 0)
        longest = np.maximum(longest, cur)
    df["longest_streak"] = longest
    df["current_streak"] = cur

    # Постоянство по неделям (недели с понедельника)
    codes = (np.asarray((days - days[0]).days) + days[0].weekday()) // 7
    W = int(codes.max()) + 1
    wk = np.zeros(U, dtype=int)
    for w in range(W):
        wk += act[:, codes == w].any(axis=1)
    df["weeks_active"] = wk
    df["weeks_share"] = wk / W

    # «Пачками»: три самых активных дня дают основную часть сообщений
    top3 = np.sort(vals, axis=1)[:, -3:].sum(axis=1)
    burst_share = np.where(n > 0, top3 / np.maximum(n, 1), 0.0)
    bursty = (ad >= MIN_USER_ACTIVE_DAYS_TREND) & (burst_share >= BURST_TOP3_SHARE)
    df["burst_share"] = burst_share

    # Тренд: последние w дней против предыдущих w дней
    if D >= MIN_TREND_DAYS:
        w = min(14, D // 2)
        last_n = vals[:, -w:].sum(axis=1).astype(float)
        prev_n = vals[:, -2 * w:-w].sum(axis=1).astype(float)
        ratio = np.where(prev_n > 0, last_n / np.where(prev_n > 0, prev_n, 1.0), np.nan)
        few = (n < MIN_USER_MSGS_TREND) | (ad < MIN_USER_ACTIVE_DAYS_TREND)
        silent = df["days_since_last"].values >= DORMANT_DAYS
        appeared = (prev_n == 0) & (last_n > 0)
        trend = np.select(
            [few, silent, bursty, appeared & (first >= D - w), appeared,
             ratio >= TREND_UP, ratio <= TREND_DOWN],
            ["мало данных", "притих", "пачками", "новый в окне", "вернулся",
             "растёт", "снижается"],
            default="стабильно")
        ratio = np.where(trend == "притих", 0.0, ratio)
        df["trend_ratio"] = ratio
        df["trend"] = trend
        df["trend_window"] = w
        df["recent_n"] = last_n
        df["prev_n"] = prev_n
    else:
        df["trend_ratio"] = np.nan
        df["trend"] = "н/д (короткое окно)"
        df["trend_window"] = np.nan
        df["recent_n"] = np.nan
        df["prev_n"] = np.nan

    # Имена, роль, ответы
    info = ds.users.drop_duplicates("uid").set_index("uid")
    name = info["name"].reindex(uids).fillna("")
    df["name"] = np.where(name.values == "", uids.values, name.values)
    df["username"] = info["username"].reindex(uids).fillna("").values
    df["avatar"] = info["avatar"].reindex(uids).fillna("").values
    df["role"] = info["role"].reindex(uids).fillna("").values
    rep = info["replies"].reindex(uids).fillna(0).values.astype(int)
    df["replies"] = rep
    df["reply_share"] = rep / np.maximum(n, 1) if ds.full_data else np.nan
    df["is_staff"] = uids.isin(ds.staff["uid"]) if len(ds.staff) else False

    # Каналы (ветки считаются вместе с родительским каналом)
    for c in ("channels_used", "top_channel", "top_channel_share", "channel_entropy"):
        df[c] = np.nan if c != "top_channel" else ""
    ch = grouped_channels(ds)
    if ch is not None and len(ch):
        ch = ch[ch["n"] > 0]
    if ch is not None and len(ch):
        ch = (ch.groupby(["uid", "group_id", "group_label"], as_index=False)["n"].sum()
                .rename(columns={"group_label": "channel"}))
        ch["tot"] = ch.groupby("uid")["n"].transform("sum")
        ch["p"] = ch["n"] / ch["tot"]
        ch["plogp"] = -(ch["p"] * np.log(ch["p"]))
        k = ch.groupby("uid").size()
        ent = ch.groupby("uid")["plogp"].sum()
        norm = (ent / np.log(k.where(k > 1))).fillna(0.0)
        top = (ch.sort_values(["uid", "n"], ascending=[True, False])
                 .drop_duplicates("uid").set_index("uid"))
        df["channels_used"] = k.reindex(uids).values
        df["top_channel"] = top["channel"].reindex(uids).fillna("").values
        df["top_channel_share"] = top["p"].reindex(uids).values
        df["channel_entropy"] = norm.reindex(uids).values

    # Время суток и дни недели
    for c in ("peak_hour", "peak_weekday", "night_share", "weekend_share"):
        df[c] = np.nan
    ht = ds.heat
    if len(ht):
        ph = (ht.groupby(["uid", "hour"])["n"].sum().reset_index()
                .sort_values(["uid", "n"], ascending=[True, False]).drop_duplicates("uid").set_index("uid"))
        tot_u = ht.groupby("uid")["n"].sum()
        night = ht[ht["hour"] < NIGHT_HOURS].groupby("uid")["n"].sum()
        wkend = ht[ht["weekday"] >= 5].groupby("uid")["n"].sum()
        df["peak_hour"] = ph["hour"].reindex(uids).values
        df["night_share"] = (night.reindex(uids).fillna(0) / tot_u.reindex(uids)).values
        df["weekend_share"] = (wkend.reindex(uids).fillna(0) / tot_u.reindex(uids)).values

    # Привычный день недели: по числу АКТИВНЫХ ДНЕЙ (а не сообщений), иначе один день решает всё
    wd_codes = np.asarray(days.weekday)
    wd_days = np.stack([act[:, wd_codes == k].sum(axis=1) for k in range(7)], axis=1)
    wd_msgs = np.stack([vals[:, wd_codes == k].sum(axis=1) for k in range(7)], axis=1)
    pw = (wd_days.astype(np.int64) * 1_000_000_000 + wd_msgs).argmax(axis=1).astype(float)
    pw = np.where(wd_days.max(axis=1) >= 2, pw, np.nan)    # без повторяемости закономерности нет
    df["peak_weekday"] = np.where(bursty, np.nan, pw)      # у «пачечных» день недели не показываем

    # Место на сервере
    total = float(n.sum())
    df["server_share"] = n / total if total else 0.0
    df["rank"] = df["messages"].rank(method="min", ascending=False).astype(int)
    df["percentile"] = df["messages"].rank(pct=True)

    # Достоверность личных выводов
    lv = np.where((n >= 50) & (ad >= 7), 2, np.where((n >= 10) & (ad >= 3), 1, 0))
    if D < MIN_TREND_DAYS:
        lv = np.minimum(lv, 1)
    if ds.incomplete or ds.truncated:
        lv = np.maximum(lv - 1, 0)
    df["reliability"] = [LEVELS[i] for i in lv]

    df = df.join(reply_metrics(ds, df.index))
    return df.sort_values(["messages", "name"], ascending=[False, True])

# ---------- Эмодзи ----------

EMOJI_MIN_MSGS = 20        # меньше сообщений — человек не попадает в «любителей эмодзи»
EMOJI_TOP = 15             # строк в основных топах
EMOJI_LOVERS_TOP = 10      # строк в таблице любителей

def emoji_user_metrics(ds: Dataset, um: pd.DataFrame) -> pd.DataFrame:
    """Личные эмодзи-метрики с тем же индексом, что у um. Нет данных — колонки пустые."""
    cols = ["emoji_uses", "emoji_distinct", "emoji_only_msgs", "emoji_only_share", "emoji_per_msg",
            "content_msgs", "content_days", "emoji_top_share"]
    out = pd.DataFrame(index=um.index)
    for c in cols:
        out[c] = np.nan
    out["emoji_top_key"] = ""
    out["emoji_top_label"] = ""
    if um.empty or not getattr(ds, "has_emoji", False):
        return out

    n = um["messages"].astype(float)
    eu, ed = ds.emoji_user, ds.emoji_days
    info = info_map(ds.emoji_info)

    uses = eu.groupby("uid")["n"].sum() if len(eu) else pd.Series(dtype=float)
    distinct = eu.groupby("uid")["key"].nunique() if len(eu) else pd.Series(dtype=float)
    only_msgs = ed.groupby("uid")["n"].sum() if len(ed) else pd.Series(dtype=float)

    out["emoji_uses"] = uses.reindex(um.index).fillna(0)
    out["emoji_distinct"] = distinct.reindex(um.index).fillna(0)
    om = np.minimum(only_msgs.reindex(um.index).fillna(0), n)
    out["emoji_only_msgs"] = om
    out["emoji_only_share"] = om / n.where(n > 0)
    out["emoji_per_msg"] = out["emoji_uses"] / n.where(n > 0)
    out["content_msgs"] = n - om

    # Активные дни «с текстом»: в день было хотя бы одно сообщение, не состоящее только из эмодзи
    daily = ds.daily
    if len(daily):
        if len(ed):
            m = daily.merge(ed.rename(columns={"n": "eo"}), on=["uid", "date"], how="left")
            m["eo"] = m["eo"].fillna(0)
            cd = ((m["n"] - m["eo"]) > 0).groupby(m["uid"]).sum()
        else:
            cd = (daily["n"] > 0).groupby(daily["uid"]).sum()
        out["content_days"] = cd.reindex(um.index).fillna(0)

    # Любимый эмодзи
    if len(eu):
        top = (eu.groupby(["uid", "key"], as_index=False)["n"].sum()
                 .sort_values(["uid", "n", "key"], ascending=[True, False, True])
                 .drop_duplicates("uid").set_index("uid"))
        keys = top["key"].reindex(um.index).fillna("")
        out["emoji_top_key"] = keys.values
        out["emoji_top_share"] = (top["n"] / uses).reindex(um.index).values
        out["emoji_top_label"] = [label_text(k, info) if k else "" for k in keys.values]
    return out

def emoji_server_metrics(ds: Dataset, um: pd.DataFrame, sm: dict = None) -> dict:
    """Метрики эмодзи по серверу. {'available': False} — файл старый, раздел скрывается."""
    if not getattr(ds, "has_emoji", False):
        return {"available": False}

    eu, info = ds.emoji_user, info_map(ds.emoji_info)
    total_msgs = int(ds.total_messages)
    only_msgs = min(int(ds.emoji_days["n"].sum()) if len(ds.emoji_days) else 0, total_msgs)
    uses_total = int(eu["n"].sum()) if len(eu) else 0

    notes = [
        "эмодзи считаются во всех сообщениях; «только эмодзи» — сообщение, где кроме них ничего нет",
        "оттенки кожи, вариации и гендерные формы одного эмодзи объединены",
        "один и тот же эмодзи в одном сообщении засчитывается один раз",
        "«внешний» — кастомный эмодзи, которого нет в списке эмодзи сервера "
        "(в суммарном файле — ни на одном из серверов бота)",
        "реакции, стикеры и текстовые смайлы (:) ) не учитываются",
    ]
    if uses_total == 0 and total_msgs >= 100:
        notes.append("эмодзи не найдено: если это неожиданно, проверьте Message Content Intent бота")
    rel = make_rel(_lvl(total_msgs, 300, 2000), notes)
    if sm and not sm.get("empty"):
        rel = _cap(rel, LEVELS.index(sm["reliability"]["level"]))

    out = {
        "available": True, "empty": uses_total == 0, "rel": rel,
        "messages": total_msgs, "uses": uses_total,
        "only_msgs": only_msgs, "only_share": (only_msgs / total_msgs) if total_msgs else 0.0,
        "distinct": 0, "categories": [], "table": None,
        "top": None, "top_own": None, "top_ext": None, "top_users": None,
        "users_with": 0, "users_with_share": 0.0, "lovers": None,
    }
    if "emoji_uses" in um.columns and len(um):
        out["users_with"] = int((um["emoji_uses"] > 0).sum())
        out["users_with_share"] = out["users_with"] / len(um)
    if uses_total == 0:
        return out

    g = eu.groupby("key").agg(uses=("n", "sum"), users=("uid", "nunique"))
    only_uses = eu[eu["only"] == 1].groupby("key")["n"].sum()
    g["only_uses"] = only_uses.reindex(g.index).fillna(0).astype(int)
    g["kind"] = ["custom" if k.startswith("c:") else "unicode" for k in g.index]
    g["external"] = [bool(info.get(k, ("", 0, 0))[2]) for k in g.index]
    g["share"] = g["uses"] / uses_total
    g = g.sort_values(["uses", "users"], ascending=[False, False], kind="stable")

    is_c = g["kind"] == "custom"
    cats = [("Обычные (Unicode)", g[~is_c]),
            ("Свои кастомные", g[is_c & ~g["external"]]),
            ("Внешние кастомные (внешний)", g[is_c & g["external"]])]
    out["categories"] = [(name, len(p), int(p["uses"].sum()), float(p["uses"].sum()) / uses_total)
                         for name, p in cats]
    out["distinct"] = len(g)
    out["table"] = g
    out["top"] = g.head(EMOJI_TOP)
    out["top_own"] = g[is_c & ~g["external"]].head(10)
    out["top_ext"] = g[is_c & g["external"]].head(10)
    out["top_users"] = g.sort_values(["users", "uses"], ascending=[False, False],
                                     kind="stable").head(EMOJI_TOP)

    if "emoji_per_msg" in um.columns and len(um):
        cand = um[(um["messages"] >= EMOJI_MIN_MSGS) & (um["emoji_uses"] > 0)]
        out["lovers"] = cand.sort_values(["emoji_per_msg", "messages"],
                                         ascending=[False, False]).head(EMOJI_LOVERS_TOP)
    return out

# ---------- Реакции ----------

REACTION_MIN_MSGS = 20        # меньше сообщений — человек не попадает в рейтинг «реакций на сообщение»
REACTION_TOP = 15             # строк в основных топах эмодзи
REACTION_PEOPLE_TOP = 10      # строк в таблицах людей

def names_map(ds: Dataset) -> dict:
    """uid -> имя: сначала авторы сообщений, затем люди, которые только ставили реакции."""
    names = {}
    if len(ds.users):
        names = ds.users.drop_duplicates("uid").set_index("uid")["name"].to_dict()
    for u, nm in getattr(ds, "reaction_names", {}).items():
        if not names.get(u):
            names[u] = nm
    return names

def reaction_user_metrics(ds: Dataset, um: pd.DataFrame) -> pd.DataFrame:
    """Личные метрики реакций с тем же индексом, что у um. Нет данных — колонки пустые (NaN)."""
    num_cols = ["rx_recv", "rx_msgs", "rx_msg_share", "rx_per_msg", "rx_distinct", "rx_top_share",
                "rx_given", "rx_given_distinct", "rx_given_targets"]
    out = pd.DataFrame(index=um.index)
    for c in num_cols:
        out[c] = np.nan
    for c in ("rx_top_key", "rx_top_label", "rx_given_top_key", "rx_given_top_label"):
        out[c] = ""
    if um.empty or not getattr(ds, "has_reactions", False):
        return out

    n = um["messages"].astype(float)
    info = info_map(ds.emoji_info)
    rc, rm = ds.reaction_recv, ds.reaction_msgs

    recv = rc.groupby("uid")["n"].sum() if len(rc) else pd.Series(dtype=float)
    distinct = rc.groupby("uid")["key"].nunique() if len(rc) else pd.Series(dtype=float)
    msgs = rm.groupby("uid")["n"].sum() if len(rm) else pd.Series(dtype=float)

    out["rx_recv"] = recv.reindex(um.index).fillna(0)
    out["rx_distinct"] = distinct.reindex(um.index).fillna(0)
    mm = np.minimum(msgs.reindex(um.index).fillna(0), n)
    out["rx_msgs"] = mm
    out["rx_msg_share"] = mm / n.where(n > 0)
    out["rx_per_msg"] = out["rx_recv"] / n.where(n > 0)

    if len(rc):
        top = (rc.sort_values(["uid", "n", "key"], ascending=[True, False, True])
                 .drop_duplicates("uid").set_index("uid"))
        keys = top["key"].reindex(um.index).fillna("")
        out["rx_top_key"] = keys.values
        out["rx_top_share"] = (top["n"] / recv).reindex(um.index).values
        out["rx_top_label"] = [label_text(k, info) if k else "" for k in keys.values]

    if ds.reactions_full:
        gv, pr = ds.reaction_given, ds.reaction_pairs
        given = gv.groupby("uid")["n"].sum() if len(gv) else pd.Series(dtype=float)
        g_distinct = gv.groupby("uid")["key"].nunique() if len(gv) else pd.Series(dtype=float)
        targets = pr.groupby("src")["dst"].nunique() if len(pr) else pd.Series(dtype=float)
        out["rx_given"] = given.reindex(um.index).fillna(0)
        out["rx_given_distinct"] = g_distinct.reindex(um.index).fillna(0)
        out["rx_given_targets"] = targets.reindex(um.index).fillna(0)
        if len(gv):
            top = (gv.sort_values(["uid", "n", "key"], ascending=[True, False, True])
                     .drop_duplicates("uid").set_index("uid"))
            keys = top["key"].reindex(um.index).fillna("")
            out["rx_given_top_key"] = keys.values
            out["rx_given_top_label"] = [label_text(k, info) if k else "" for k in keys.values]
    return out

def reaction_server_metrics(ds: Dataset, um: pd.DataFrame, sm: dict = None) -> dict:
    """Метрики реакций по серверу. {'available': False} — реакции не собирались, раздел скрывается."""
    if not getattr(ds, "has_reactions", False):
        return {"available": False}

    rc, info = ds.reaction_recv, info_map(ds.emoji_info)
    full = bool(ds.reactions_full)
    total_msgs = int(ds.total_messages)
    uses = int(rc["n"].sum()) if len(rc) else 0
    msgs_with = int(ds.reaction_msgs["n"].sum()) if len(ds.reaction_msgs) else 0
    msgs_with = min(msgs_with, total_msgs)

    notes = [
        "реакции — снимок на момент сбора: время реакции неизвестно, поэтому они привязаны ко дню сообщения",
        "у свежих сообщений реакций накопилось меньше, чем у старых: сравнивать начало и конец окна нельзя",
        "реакции на сообщения ботов, системные сообщения и уже снятые реакции не учитываются",
        "оттенки кожи, вариации и гендерные формы одного эмодзи объединены; "
        "«внешний» — кастомный эмодзи, которого нет в списке эмодзи сервера",
    ]
    if full:
        notes.append(f"режим «полный»: реакции ботов и самореакции исключены (их было {ds.reaction_self_or_bot})")
        if ds.reaction_unattributed:
            notes.append(f"у {ds.reaction_unattributed} реакций автор неизвестен (супер-реакции, лимит или ошибка): "
                         "они учтены как «получено», но не очищены от ботов и не входят в «поставлено»")
        if ds.reaction_capped:
            notes.append("достигнут лимит запросов: часть реакций собрана только счётчиками")
        if ds.reaction_errors:
            notes.append(f"ошибок при чтении авторов реакций: {ds.reaction_errors}")
    else:
        notes.append("режим «счётчики»: в числа входят реакции ботов и самореакции авторов, "
                     "а кто ставил реакции — неизвестно (для этого нужен режим «полный»)")
    if uses == 0 and total_msgs >= 100:
        notes.append("реакций не найдено: если это неожиданно, проверьте права бота на чтение истории")

    rel = make_rel(_lvl(total_msgs, 300, 2000), notes)
    if sm and not sm.get("empty"):
        rel = _cap(rel, LEVELS.index(sm["reliability"]["level"]))

    out = {
        "available": True, "empty": uses == 0, "full": full, "rel": rel,
        "messages": total_msgs, "uses": uses, "msgs_with": msgs_with,
        "msgs_share": (msgs_with / total_msgs) if total_msgs else 0.0,
        "per_msg": (uses / total_msgs) if total_msgs else 0.0,
        "distinct": 0, "categories": [], "table": None,
        "top": None, "top_own": None, "top_ext": None,
        "users_with": 0, "users_with_share": 0.0,
        "top_received": None, "top_rate": None,
        "givers": None, "givers_total": 0, "pairs": None,
    }
    if "rx_recv" in um.columns and len(um):
        out["users_with"] = int((um["rx_recv"] > 0).sum())
        out["users_with_share"] = out["users_with"] / len(um)
    if uses == 0:
        return out

    g = rc.groupby("key").agg(uses=("n", "sum"), users=("uid", "nunique"))
    eu = ds.emoji_user
    text_uses = eu.groupby("key")["n"].sum() if len(eu) else pd.Series(dtype=float)
    g["text_uses"] = text_uses.reindex(g.index).fillna(0).astype(int)
    if full and len(ds.reaction_given):
        g["givers"] = (ds.reaction_given.groupby("key")["uid"].nunique()
                       .reindex(g.index).fillna(0).astype(int))
    g["kind"] = ["custom" if k.startswith("c:") else "unicode" for k in g.index]
    g["external"] = [bool(info.get(k, ("", 0, 0))[2]) for k in g.index]
    g["share"] = g["uses"] / uses
    g = g.sort_values(["uses", "users"], ascending=[False, False], kind="stable")

    is_c = g["kind"] == "custom"
    cats = [("Обычные (Unicode)", g[~is_c]),
            ("Свои кастомные", g[is_c & ~g["external"]]),
            ("Внешние кастомные (внешний)", g[is_c & g["external"]])]
    out["categories"] = [(name, len(p), int(p["uses"].sum()), float(p["uses"].sum()) / uses)
                         for name, p in cats]
    out["distinct"] = len(g)
    out["table"] = g
    out["top"] = g.head(REACTION_TOP)
    out["top_own"] = g[is_c & ~g["external"]].head(10)
    out["top_ext"] = g[is_c & g["external"]].head(10)

    if "rx_recv" in um.columns and len(um):
        got = um[um["rx_recv"] > 0]
        out["top_received"] = got.sort_values(["rx_recv", "messages"], ascending=[False, False]
                                              ).head(REACTION_PEOPLE_TOP)
        cand = got[got["messages"] >= REACTION_MIN_MSGS]
        out["top_rate"] = cand.sort_values(["rx_per_msg", "messages"], ascending=[False, False]
                                           ).head(REACTION_PEOPLE_TOP)

    if full:
        names = names_map(ds)
        pr = ds.reaction_pairs
        if len(ds.reaction_given):
            gv = ds.reaction_given.groupby("uid").agg(given=("n", "sum"), distinct=("key", "nunique"))
            gv["targets"] = (pr.groupby("src")["dst"].nunique().reindex(gv.index).fillna(0).astype(int)
                             if len(pr) else 0)
            gv["name"] = [names.get(u) or u for u in gv.index]
            out["givers_total"] = len(gv)
            out["givers"] = gv.sort_values(["given", "distinct"], ascending=[False, False],
                                           kind="stable").head(REACTION_PEOPLE_TOP)
        if len(pr):
            top_pairs = pr.sort_values("n", ascending=False, kind="stable").head(REACTION_PEOPLE_TOP).copy()
            top_pairs["src_name"] = [names.get(u) or u for u in top_pairs["src"]]
            top_pairs["dst_name"] = [names.get(u) or u for u in top_pairs["dst"]]
            out["pairs"] = top_pairs
    return out

# ---------- Присутствие и роли ----------

def presence_user_metrics(ds: Dataset, um: pd.DataFrame) -> pd.DataFrame:
    """Личные факты: на сервере ли человек, когда вступил/ушёл, какие у него роли. Нет снимка — пусто."""
    cols = ["in_server", "joined", "left_date", "first_seen", "server_roles"]
    out = pd.DataFrame(index=um.index, columns=cols, dtype=object)
    out["server_roles"] = ""
    if um.empty:
        return out

    if getattr(ds, "has_presence", False) and len(ds.presence):
        partial = any(si.snapshot_partial for si in ds.sync_info.values())
        agg: dict = {}
        for r in ds.presence.itertuples(index=False):
            a = agg.setdefault(r.uid, {"present": 0, "joined": None, "first": None, "left": None})
            a["present"] = max(a["present"], int(r.present))
            if _ok(r.joined) and (a["joined"] is None or r.joined < a["joined"]):
                a["joined"] = r.joined
            if _ok(r.first_seen) and (a["first"] is None or r.first_seen < a["first"]):
                a["first"] = r.first_seen
            if _ok(r.left) and (a["left"] is None or r.left > a["left"]):
                a["left"] = r.left
        for uid in um.index:
            a = agg.get(uid)
            if a is None:                          # бот никогда не видел человека на сервере
                out.at[uid, "in_server"] = None if partial else False
                continue
            here = a["present"] == 1
            out.at[uid, "in_server"] = here
            out.at[uid, "joined"] = a["joined"]
            out.at[uid, "first_seen"] = a["first"]
            out.at[uid, "left_date"] = None if here else a["left"]

    if getattr(ds, "has_roles", False) and len(ds.member_roles):
        multi = ds.member_roles["server"].nunique() > 1
        text: dict = {}
        for r in ds.member_roles.itertuples(index=False):
            if r.uid in out.index and r.roles:
                chunk = ", ".join(r.roles)
                text.setdefault(r.uid, []).append(f"{r.server}: {chunk}" if multi else chunk)
        for uid, parts in text.items():
            out.at[uid, "server_roles"] = "; ".join(parts)
    return out