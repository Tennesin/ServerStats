"""Построение отчётов (xlsx и txt) из ScanResult. С Discord напрямую не работает."""

import io
from typing import Optional
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import xlsxwriter

from config import (
    AUDIT_LABELS, AUDIT_RETENTION_DAYS, DATE_FMT, MAX_LIST_ROWS, MAX_MATRIX_COLS, NAME_WIDTH,
    REACTION_LABELS, SORT_LABELS, TZ, TZ_NAME, clean, fit_name, fmt_list, fmt_num,
)
from scanner import ScanParams, ScanResult, ScanState, chan_label

TOP_CHANNELS = 15
TOP_USERS = 10
TABLE_USERS = 15
MATRIX_USERS = 30
COMPARE_ROWS = 500

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
KIND_RU = {"owner": "Основатель", "admin": "Админ"}
EVENT_LABELS = {
    "admin_grant": "Выдана админская роль",
    "admin_revoke": "Снята админская роль",
    "owner_transfer": "Передача прав Основателя",
}
AUDIT_STATUS = {
    "off": "журнал аудита не собирался",
    "ok": "собран",
    "denied": "нет доступа: боту нужно право «Просмотр журнала аудита»",
    "failed": "сбор прервался ошибкой, данные неполные",
    "partial": "собран не по всем серверам (подробности на листе «Служебное»)",
}
AUDIT_MODE_CODE = {"off": 0, "ok": 1, "denied": 2, "failed": 3, "partial": 4}
AUDIT_UNKNOWN_ACTOR = "(автор не определён)"
MODE_TITLES = {
    "days": "по дням",
    "weeks": "по неделям (в шапке — понедельник недели)",
    "months": "по месяцам",
}

# ---------- Агрегация ----------

@dataclass
class Agg:
    order: list
    totals: dict
    per_user: dict
    names: dict
    usernames: dict = field(default_factory=dict)
    merged: bool = False
    staff: Optional[object] = None
    staff_servers: dict = field(default_factory=dict)
    present: set = field(default_factory=set)
    presence_servers: dict = field(default_factory=dict)

    def name_of(self, uid: int) -> str:
        nick = self.names.get(uid, str(uid))
        if self.merged:
            return nick                      # суммарная статистика: просто ник
        original = self.usernames.get(uid)
        return f"{nick} [{original}]" if original else nick

    def role_of(self, uid: int) -> str:
        """Должность для колонки «Роль»; в суммарном файле — по каждому серверу."""
        if self.staff_servers:
            return ", ".join(
                f"{KIND_RU[i.members[uid].kind]} ({srv})"
                for srv, i in self.staff_servers.items() if uid in i.members
            )
        if self.staff and uid in self.staff.members:
            return KIND_RU[self.staff.members[uid].kind]
        return ""

    def status_of(self, uid: int) -> str:
        """Одиночный отчёт: «Присутствует» / «Нет в сервере».
        Суммарный: список серверов, где человек сейчас есть, иначе «-»."""
        if self.merged:
            here = [srv for srv, ids in self.presence_servers.items() if uid in ids]
            return ", ".join(here) if here else "-"
        return "Присутствует" if uid in self.present else "Нет в сервере"

def aggregate(params: ScanParams, result: ScanResult) -> Agg:
    per_user: dict[int, dict[date, int]] = defaultdict(dict)
    totals: dict[int, int] = defaultdict(int)
    for (uid, day), n in result.user_day.items():
        per_user[uid][day] = n
        totals[uid] += n
    names = result.names

    def nm(uid):
        return names.get(uid, str(uid)).casefold()

    if params.sort_key == "name":
        order = sorted(totals, key=lambda u: (nm(u), u))
    else:
        order = sorted(totals, key=lambda u: (-totals[u], nm(u), u))
    return Agg(order, totals, per_user, names, result.usernames, result.merged,
               result.staff, result.staff_servers,
               present=result.present, presence_servers=result.presence_servers)

def period_text(params: ScanParams) -> str:
    s = params.start_date.strftime(DATE_FMT) if params.start_date else "с начала истории"
    e = params.end_date.strftime(DATE_FMT) if params.end_date else "по сегодня"
    if params.period_label:
        return f"{params.period_label} ({s} — {e})"
    return f"{s} — {e}"

def prev_period_text(params: ScanParams) -> str:
    pr = params.prev_range
    return f"{pr[0].strftime(DATE_FMT)} — {pr[1].strftime(DATE_FMT)}" if pr else ""

def staff_blocks(result: ScanResult, guild_name: str) -> list:
    """[(название сервера, StaffInfo)] — один блок для сервера или по блоку на каждый в сумме."""
    if result.staff_servers:
        return list(result.staff_servers.items())
    return [(guild_name, result.staff)] if result.staff else []

def staff_rows(agg: Agg, result: ScanResult, srv: str, info):
    """Сортировка: основатель первым, затем админы по убыванию сообщений.
    Возвращает ([(uid, member, сообщений)], всего сообщений на сервере)."""
    if result.merged:
        def msgs(uid): return result.user_server.get((uid, srv), 0)
        total = sum(n for (_u, s), n in result.user_server.items() if s == srv)
    else:
        def msgs(uid): return agg.totals.get(uid, 0)
        total = sum(agg.totals.values())
    rows = [(uid, mb, msgs(uid)) for uid, mb in info.members.items()]
    rows.sort(key=lambda r: (r[1].kind != "owner", -r[2], r[1].name.casefold()))
    return rows, total

def staff_label(mb) -> str:
    return f"{mb.name} [{mb.username}]" if mb.username else mb.name

def event_text(e) -> str:
    base = EVENT_LABELS.get(e.kind, e.kind)
    return f"{base} «{e.role}»" if e.role else base

def event_person(agg: Agg, result: ScanResult, uid: int) -> str:
    if not uid:
        return "-"
    return clean(result.event_names.get(uid) or agg.names.get(uid) or str(uid))

def write_admin_events(ws, F, row: int, agg: Agg, result: ScanResult, srv: str) -> int:
    """Список «Событие | Дата | Участник | Участник 2» под блоком сервера. Возвращает следующую строку."""
    ws.merge_range(row, 0, row, 3, f"Ключевые события администрации: {srv}", F["section"])
    row += 1
    if result.audit_mode == "off":
        ws.write_string(row, 0, "События не собирались: журнал аудита отключён или не собирался.", F["note"])
        return row + 2
    events = [e for e in result.admin_events if (not result.merged) or e.server == srv]
    if not events:
        ws.write_string(row, 0, "За выбранный период ключевых событий не найдено.", F["note"])
        return row + 2
    for c, h in enumerate(["Событие", "Дата", "Участник", "Участник 2"]):
        ws.write_string(row, c, h, F["head"])
    row += 1
    for e in events:
        ws.write_string(row, 0, clean(event_text(e)), F["text"])
        ws.write_datetime(row, 1, e.day, F["date"])
        ws.write_string(row, 2, event_person(agg, result, e.uid), F["text"])
        ws.write_string(row, 3, event_person(agg, result, e.uid2), F["text"])
        row += 1
    ws.write_string(row, 0, "Участник — кому выдали / у кого сняли роль (при передаче — прежний основатель); "
                            "Участник 2 — кто это сделал (при передаче — новый основатель); «-» — неизвестно.",
                    F["note"])
    return row + 2

# ---------- Детализация матрицы ----------

def bucket_of(day: date, mode: str) -> date:
    if mode == "weeks":
        return day - timedelta(days=day.weekday())
    if mode == "months":
        return day.replace(day=1)
    return day

def next_bucket(b: date, mode: str) -> date:
    if mode == "days":
        return b + timedelta(days=1)
    if mode == "weeks":
        return b + timedelta(days=7)
    return (b.replace(day=28) + timedelta(days=4)).replace(day=1)

def make_buckets(first: date, last: date, mode: str) -> list[date]:
    out, b, end = [], bucket_of(first, mode), bucket_of(last, mode)
    while b <= end:
        out.append(b)
        b = next_bucket(b, mode)
    return out

def pick_detail(requested: str, first: date, last: date):
    span = (last - first).days + 1
    if requested in ("days", "weeks", "months"):
        mode, reason = requested, "задано вручную"
    else:
        mode = "days" if span <= 90 else "weeks" if span <= 730 else "months"
        reason = f"выбрано автоматически (период {span} дн.)"
    buckets = make_buckets(first, last, mode)
    while len(buckets) > MAX_MATRIX_COLS and mode != "months":
        mode = "weeks" if mode == "days" else "months"
        reason = f"укрупнено, чтобы матрица не вышла за {MAX_MATRIX_COLS} столбцов"
        buckets = make_buckets(first, last, mode)
    return mode, buckets, reason

# ---------- Excel ----------

def make_formats(wb):
    border = {"border": 1, "border_color": "#D9D9D9", "valign": "vcenter"}
    return {
        "head": wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF",
                               "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True}),
        "text": wb.add_format({**border}),
        "int": wb.add_format({**border, "num_format": "#,##0"}),
        "dec": wb.add_format({**border, "num_format": "0.00"}),
        "pct": wb.add_format({**border, "num_format": "0.0%"}),
        "delta": wb.add_format({**border, "num_format": "+#,##0;-#,##0;0"}),
        "delta_pct": wb.add_format({**border, "num_format": "+0.0%;-0.0%;0.0%"}),
        "date": wb.add_format({**border, "num_format": "dd.mm.yyyy", "align": "center"}),
        "cell": wb.add_format({**border, "align": "center"}),
        "tot_text": wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1, "valign": "vcenter"}),
        "tot_int": wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1,
                                  "num_format": "#,##0", "valign": "vcenter"}),
        "tot_cell": wb.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1,
                                   "align": "center", "valign": "vcenter"}),
        "banner": wb.add_format({"bold": True, "bg_color": "#FFC7CE", "font_color": "#9C0006",
                                 "valign": "vcenter"}),
        "bold": wb.add_format({"bold": True}),
        "section": wb.add_format({"bold": True, "bg_color": "#DDEBF7"}),
        "note": wb.add_format({"italic": True, "font_color": "#666666"}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
    }

def sheet_summary(wb, F, agg: Agg, params, state, name_w):
    order, totals, per_user = agg.order, agg.totals, agg.per_user
    grand_total = sum(totals.values())
    ws = wb.add_worksheet("Сводка")
    top = 0
    if state.cancelled:
        ws.merge_range(0, 0, 0, 8, "⚠ Сбор остановлен вручную: данные НЕПОЛНЫЕ.", F["banner"])
        top = 1
    headers = ["№", "Пользователь", "ID", "Сообщений", "Дней с сообщениями",
               "Среднее в активный день", "Первая дата", "Последняя дата", "Доля от всех", "Роль",
               "Статус"]
    for c, h in enumerate(headers):
        ws.write_string(top, c, h, F["head"])
    ws.set_row(top, 32)
    ws.set_column(0, 0, 6)
    ws.set_column(1, 1, name_w)
    ws.set_column(2, 2, 21)
    ws.set_column(3, 5, 14)
    ws.set_column(6, 7, 14)
    ws.set_column(8, 8, 12)
    ws.set_column(9, 9, 28)
    ws.set_column(10, 10, 40 if agg.merged else 16)

    for i, uid in enumerate(order, start=1):
        r = top + i
        days = per_user[uid]
        n = totals[uid]
        ws.write_number(r, 0, i, F["int"])
        ws.write_string(r, 1, clean(agg.name_of(uid)), F["text"])
        ws.write_string(r, 2, str(uid), F["text"])
        ws.write_number(r, 3, n, F["int"])
        ws.write_number(r, 4, len(days), F["int"])
        ws.write_number(r, 5, n / len(days), F["dec"])
        ws.write_datetime(r, 6, min(days), F["date"])
        ws.write_datetime(r, 7, max(days), F["date"])
        ws.write_number(r, 8, n / grand_total if grand_total else 0, F["pct"])
        ws.write_string(r, 9, agg.role_of(uid), F["text"])
        ws.write_string(r, 10, clean(agg.status_of(uid)), F["text"])
    if order:
        last_row = top + len(order)
        ws.autofilter(top, 0, last_row, len(headers) - 1)
        tr = last_row + 2
        ws.write_string(tr, 1, "ВСЕГО", F["tot_text"])
        ws.write_number(tr, 3, grand_total, F["tot_int"])
        ws.write_string(tr, 2, f"пользователей: {len(order)}", F["tot_text"])
    else:
        ws.write_string(top + 1, 1, "Сообщений за выбранный период не найдено.")
    ws.freeze_panes(top + 1, 2)

def sheet_matrix(wb, F, agg: Agg, params, name_w) -> str:
    order, totals, per_user = agg.order, agg.totals, agg.per_user
    grand_total = sum(totals.values())
    wm = wb.add_worksheet("Матрица")
    all_days = [d for u in order for d in per_user[u]]
    if not all_days:
        wm.write_string(0, 0, "Сообщений за выбранный период не найдено.")
        return "матрица пуста"
    mode, buckets, reason = pick_detail(params.detail, min(all_days), max(all_days))
    col_of = {b: 2 + i for i, b in enumerate(buckets)}
    head_fmt = wb.add_format({
        "bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1,
        "align": "center", "valign": "vcenter", "rotation": 90,
        "num_format": "mm.yyyy" if mode == "months" else "dd.mm.yy",
    })
    wm.set_row(0, 62)
    wm.write_string(0, 0, "Пользователь", F["head"])
    wm.write_string(0, 1, "Итого", F["head"])
    for b in buckets:
        wm.write_datetime(0, col_of[b], b, head_fmt)
    wm.set_column(0, 0, name_w)
    wm.set_column(1, 1, 10)
    wm.set_column(2, 1 + len(buckets), 8 if mode == "months" else 5.5)

    col_sums = defaultdict(int)
    for i, uid in enumerate(order, start=1):
        wm.write_string(i, 0, clean(agg.name_of(uid)), F["text"])
        wm.write_number(i, 1, totals[uid], F["int"])
        row_vals: dict[date, int] = defaultdict(int)
        for day, n in per_user[uid].items():
            row_vals[bucket_of(day, mode)] += n
        for b, n in row_vals.items():
            wm.write_number(i, col_of[b], n, F["cell"])
            col_sums[b] += n
    tr = len(order) + 1
    wm.write_string(tr, 0, "ВСЕГО", F["tot_text"])
    wm.write_number(tr, 1, grand_total, F["tot_int"])
    for b in buckets:
        if col_sums[b]:
            wm.write_number(tr, col_of[b], col_sums[b], F["tot_cell"])
        else:
            wm.write_blank(tr, col_of[b], None, F["tot_cell"])
    wm.conditional_format(1, 2, len(order), 1 + len(buckets), {
        "type": "2_color_scale", "min_color": "#FFFFFF", "max_color": "#2E9E5B",
    })
    wm.freeze_panes(1, 2)
    return f"{MODE_TITLES[mode]}; {reason}; столбцов: {len(buckets)}"

def sheet_daily(wb, F, agg: Agg, name_w) -> bool:
    wl = wb.add_worksheet("По дням (список)")
    for c, h in enumerate(["Пользователь", "ID", "Дата", "Сообщений"]):
        wl.write_string(0, c, h, F["head"])
    wl.set_column(0, 0, name_w)
    wl.set_column(1, 1, 21)
    wl.set_column(2, 3, 13)
    r = 0
    truncated = False
    for uid in agg.order:
        nm = clean(agg.name_of(uid))
        for day, n in sorted(agg.per_user[uid].items()):
            if r >= MAX_LIST_ROWS:
                truncated = True
                break
            r += 1
            wl.write_string(r, 0, nm, F["text"])
            wl.write_string(r, 1, str(uid), F["text"])
            wl.write_datetime(r, 2, day, F["date"])
            wl.write_number(r, 3, n, F["int"])
        if truncated:
            break
    wl.autofilter(0, 0, max(r, 1), 3)
    wl.freeze_panes(1, 0)
    return truncated

def sheet_analytics(wb, F, agg: Agg, result: ScanResult, params: ScanParams, name_w):
    ws = wb.add_worksheet("Аналитика")
    ws.set_column(0, 0, max(name_w, 38))
    ws.set_column(1, 26, 11)
    grand = sum(agg.totals.values())
    if not grand:
        ws.write_string(0, 0, "Сообщений за выбранный период не найдено.")
        return

    row = 0

    def section(text, note=None):
        nonlocal row
        ws.merge_range(row, 0, row, 8, text, F["section"])
        row += 1
        if note:
            ws.write_string(row, 0, note, F["note"])
            row += 1

    def lab(cid):
        return clean(result.channel_names.get(cid, str(cid)))

    chan_total: dict = defaultdict(int)
    chan_auth: dict = defaultdict(set)
    user_chan: dict = defaultdict(dict)
    for (uid, cid), n in result.user_channel.items():
        chan_total[cid] += n
        chan_auth[cid].add(uid)
        user_chan[uid][cid] = n
    by_count = sorted(agg.totals, key=lambda u: (-agg.totals[u], agg.name_of(u).casefold()))

    # 1. Топ каналов
    section(f"1. Топ каналов (до {TOP_CHANNELS})")
    for c, t in enumerate(["Канал", "Сообщений", "Доля", "Авторов"]):
        ws.write_string(row, c, t, F["head"])
    head_row = row
    row += 1
    first = row
    top = sorted(chan_total, key=lambda c: (-chan_total[c], lab(c).casefold()))[:TOP_CHANNELS]
    for cid in top:
        ws.write_string(row, 0, lab(cid), F["text"])
        ws.write_number(row, 1, chan_total[cid], F["int"])
        ws.write_number(row, 2, chan_total[cid] / grand, F["pct"])
        ws.write_number(row, 3, len(chan_auth[cid]), F["int"])
        row += 1
    if top:
        chart = wb.add_chart({"type": "bar"})
        chart.add_series({
            "categories": ["Аналитика", first, 0, row - 1, 0],
            "values": ["Аналитика", first, 1, row - 1, 1],
            "fill": {"color": "#2E7BB8"}, "gap": 50,
        })
        chart.set_legend({"none": True})
        chart.set_title({"name": "Топ каналов по сообщениям"})
        chart.set_y_axis({"reverse": True})
        chart.set_size({"width": 560, "height": 380})
        ws.insert_chart(head_row, 5, chart)
    row = max(row, head_row + 20) + 1

    # 2. Кто где пишет
    section(f"2. Кто где пишет (топ-{TOP_USERS} пользователей, по 3 главных канала)")
    for uid in by_count[:TOP_USERS]:
        ws.write_string(row, 0, clean(agg.name_of(uid)), F["tot_text"])
        ws.write_number(row, 1, agg.totals[uid], F["tot_int"])
        ws.write_blank(row, 2, None, F["tot_text"])
        row += 1
        for cid, n in sorted(user_chan[uid].items(), key=lambda kv: -kv[1])[:3]:
            ws.write_string(row, 0, "    " + lab(cid), F["text"])
            ws.write_number(row, 1, n, F["int"])
            ws.write_number(row, 2, n / agg.totals[uid], F["pct"])
            row += 1
    row += 1

    # 3. Тепловая карта
    section("3. Активность по дням недели и часам", f"Часовой пояс: {TZ_NAME}")
    ws.write_string(row, 0, "День \\ час", F["head"])
    for hr in range(24):
        ws.write_number(row, 1 + hr, hr, F["head"])
    row += 1
    r0 = row
    for wd in range(7):
        ws.write_string(row, 0, WEEKDAYS[wd], F["text"])
        for hr in range(24):
            ws.write_number(row, 1 + hr, result.heat.get((wd, hr), 0), F["cell"])
        row += 1
    ws.conditional_format(r0, 1, row - 1, 24, {
        "type": "2_color_scale", "min_color": "#FFFFFF", "max_color": "#E4572E",
    })
    row += 1

    # 4. Ответы
    total_replies = sum(result.replies.values())
    section("4. Ответы (reply)",
            "Вложения не считаются: для них нужен привилегированный интент Message Content.")
    ws.write_string(row, 0, "Ответов всего", F["tot_text"])
    ws.write_number(row, 1, total_replies, F["tot_int"])
    ws.write_number(row, 2, total_replies / grand, wb.add_format(
        {"bold": True, "bg_color": "#F2F2F2", "border": 1, "num_format": "0.0%"}))
    row += 1
    for c, t in enumerate(["Пользователь", "Сообщений", "Ответов", "Доля ответов"]):
        ws.write_string(row, c, t, F["head"])
    row += 1
    for uid in by_count[:TABLE_USERS]:
        rep = result.replies.get(uid, 0)
        ws.write_string(row, 0, clean(agg.name_of(uid)), F["text"])
        ws.write_number(row, 1, agg.totals[uid], F["int"])
        ws.write_number(row, 2, rep, F["int"])
        ws.write_number(row, 3, rep / agg.totals[uid], F["pct"])
        row += 1
    row += 1

    # 5. Динамика
    prev = result.prev
    if prev is not None:
        prev_tot: dict = defaultdict(int)
        for (uid, _d), n in prev.user_day.items():
            prev_tot[uid] += n
        section("5. Динамика к предыдущему периоду",
                f"Сейчас: {period_text(params)}. Раньше: {prev_period_text(params)} "
                "(такой же длины). «Новый» — не писал в прошлом периоде, «не писал» — "
                "писал раньше, но не сейчас; это не то же самое, что вступление/уход с сервера.")
        for c, t in enumerate(["Пользователь", "Сейчас", "Раньше", "Изменение", "Изм., %", "Статус"]):
            ws.write_string(row, c, t, F["head"])
        row += 1
        cur_sum, prev_sum = sum(agg.totals.values()), sum(prev_tot.values())
        ws.write_string(row, 0, "ВСЕГО", F["tot_text"])
        ws.write_number(row, 1, cur_sum, F["tot_int"])
        ws.write_number(row, 2, prev_sum, F["tot_int"])
        ws.write_number(row, 3, cur_sum - prev_sum, F["delta"])
        if prev_sum:
            ws.write_number(row, 4, (cur_sum - prev_sum) / prev_sum, F["delta_pct"])
        else:
            ws.write_blank(row, 4, None, F["tot_text"])
        ws.write_blank(row, 5, None, F["tot_text"])
        row += 1
        uids = set(agg.totals) | set(prev_tot)
        ranked = sorted(uids, key=lambda u: (-agg.totals.get(u, 0), -prev_tot.get(u, 0),
                                             agg.name_of(u).casefold()))
        for uid in ranked[:COMPARE_ROWS]:
            cur, old = agg.totals.get(uid, 0), prev_tot.get(uid, 0)
            ws.write_string(row, 0, clean(agg.name_of(uid)), F["text"])
            ws.write_number(row, 1, cur, F["int"])
            ws.write_number(row, 2, old, F["int"])
            ws.write_number(row, 3, cur - old, F["delta"])
            if old:
                ws.write_number(row, 4, (cur - old) / old, F["delta_pct"])
            else:
                ws.write_blank(row, 4, None, F["text"])
            status = "новый" if (cur and not old) else "не писал" if (old and not cur) else ""
            ws.write_string(row, 5, status, F["cell"])
            row += 1
        if len(ranked) > COMPARE_ROWS:
            ws.write_string(row, 0, f"… показаны первые {COMPARE_ROWS} из {len(ranked)}", F["note"])
            row += 1
        row += 1

    # 6. Серверы (только в суммарном файле)
    if result.user_server:
        srv_total: dict = defaultdict(int)
        srv_auth: dict = defaultdict(set)
        for (uid, srv), n in result.user_server.items():
            srv_total[srv] += n
            srv_auth[srv].add(uid)
        servers = sorted(srv_total, key=lambda s: (-srv_total[s], s.casefold()))
        section("6. Рейтинг серверов")
        for c, t in enumerate(["Сервер", "Сообщений", "Доля", "Авторов"]):
            ws.write_string(row, c, t, F["head"])
        row += 1
        for srv in servers:
            ws.write_string(row, 0, clean(srv), F["text"])
            ws.write_number(row, 1, srv_total[srv], F["int"])
            ws.write_number(row, 2, srv_total[srv] / grand, F["pct"])
            ws.write_number(row, 3, len(srv_auth[srv]), F["int"])
            row += 1
        row += 1
        section(f"7. Пользователь × сервер (топ-{MATRIX_USERS} пользователей)")
        ws.write_string(row, 0, "Пользователь", F["head"])
        for j, srv in enumerate(servers[:25]):
            ws.write_string(row, 1 + j, clean(srv), F["head"])
        ws.set_row(row, 48)
        row += 1
        m0 = row
        for uid in by_count[:MATRIX_USERS]:
            ws.write_string(row, 0, clean(agg.name_of(uid)), F["text"])
            for j, srv in enumerate(servers[:25]):
                n = result.user_server.get((uid, srv), 0)
                if n:
                    ws.write_number(row, 1 + j, n, F["cell"])
                else:
                    ws.write_blank(row, 1 + j, None, F["cell"])
            row += 1
        if row > m0:
            ws.conditional_format(m0, 1, row - 1, min(25, len(servers)), {
                "type": "2_color_scale", "min_color": "#FFFFFF", "max_color": "#2E9E5B",
            })
        if len(servers) > 25:
            ws.write_string(row, 0, f"… показаны первые 25 серверов из {len(servers)}", F["note"])
            row += 1

def sheet_staff(wb, F, agg: Agg, result: ScanResult, guild_name: str, name_w):
    ws = wb.add_worksheet("Администрация")
    ws.set_column(0, 0, max(name_w, 40))
    ws.set_column(1, 1, 21)
    ws.set_column(2, 2, 30)
    ws.set_column(3, 3, 40)
    ws.set_column(4, 5, 14)
    blocks = staff_blocks(result, guild_name)
    if not blocks:
        ws.write_string(0, 0, "Данные об администрации не собраны.")
        return

    row = 0
    for srv, info in blocks:
        rows, total = staff_rows(agg, result, srv, info)
        ws.merge_range(row, 0, row, 5, f"Сервер: {srv}", F["section"])
        row += 1
        for c, h in enumerate(["Пользователь", "ID", "Должность", "Роли администрации",
                               "Сообщений", "Доля от всех"]):
            ws.write_string(row, c, h, F["head"])
        row += 1
        staff_sum = 0
        for uid, mb, n in rows:
            staff_sum += n
            ws.write_string(row, 0, clean(staff_label(mb)), F["text"])
            ws.write_string(row, 1, str(uid), F["text"])
            ws.write_string(row, 2, KIND_RU[mb.kind], F["text"])
            ws.write_string(row, 3, clean(", ".join(mb.roles) or "—"), F["text"])
            ws.write_number(row, 4, n, F["int"])
            ws.write_number(row, 5, n / total if total else 0, F["pct"])
            row += 1
        ws.write_string(row, 0, f"Администрация всего: {len(rows)} чел.", F["tot_text"])
        ws.write_blank(row, 1, None, F["tot_text"])
        ws.write_blank(row, 2, None, F["tot_text"])
        ws.write_blank(row, 3, None, F["tot_text"])
        ws.write_number(row, 4, staff_sum, F["tot_int"])
        ws.write_number(row, 5, staff_sum / total if total else 0, F["pct"])
        row += 1
        ws.write_string(row, 0, "Остальные участники", F["text"])
        for c in (1, 2, 3):
            ws.write_blank(row, c, None, F["text"])
        ws.write_number(row, 4, total - staff_sum, F["int"])
        ws.write_number(row, 5, (total - staff_sum) / total if total else 0, F["pct"])
        row += 1
        if info.unmatched_roles:
            ws.write_string(row, 0, "Не найдены роли из ADMIN_ROLES: " + ", ".join(info.unmatched_roles), F["note"])
            row += 1
        if info.partial:
            ws.write_string(row, 0, "Список участников мог загрузиться не полностью.", F["note"])
            row += 1
        row += 1
        row = write_admin_events(ws, F, row, agg, result, srv)

def sheet_data(wb, agg: Agg, result: ScanResult):
    """Скрытые машиночитаемые листы для автономного анализатора: полные данные без обрезаний.
    ID пишутся строкой: числа в Excel теряют точность после 15 цифр."""

    def table(name, headers, rows):
        ws = wb.add_worksheet(name)
        ws.write_row(0, 0, headers)
        r = 0
        for row in rows:
            if r >= MAX_LIST_ROWS:
                break
            r += 1
            for c, v in enumerate(row):
                if isinstance(v, str):
                    ws.write_string(r, c, clean(v))
                else:
                    ws.write_number(r, c, v)
        ws.hide()

    prev_users = set()
    if result.prev is not None:
        prev_users = {u for (u, _d) in result.prev.user_day}
    extra = sorted(prev_users - set(agg.order))     # писали только в прошлом периоде

    table(
        "data_users",
        ["uid", "name", "username", "messages", "replies", "role", "avatar"],
        ((str(u), agg.names.get(u, str(u)), agg.usernames.get(u, ""),
          agg.totals.get(u, 0), result.replies.get(u, 0), agg.role_of(u),
          result.avatars.get(u, ""))
         for u in [*agg.order, *extra]),
    )
    table(
        "data_channels",
        ["uid", "channel_id", "channel", "messages"],
        ((str(u), str(cid), result.channel_names.get(cid, ""), n)
         for (u, cid), n in result.user_channel.items()),
    )
    table(
        "data_heat",
        ["uid", "weekday", "hour", "messages"],
        ((str(u), wd, hr, n) for (u, wd, hr), n in result.user_heat.items()),
    )
    table(
        "data_replies",
        ["src", "dst", "messages"],
        ((str(a), str(b), n) for (a, b), n in result.reply_pairs.items()),
    )
    table(
        "data_mentions",
        ["src", "dst", "messages"],
        ((str(a), str(b), n) for (a, b), n in result.mention_pairs.items()),
    )
    table(
        "data_reply_meta",
        ["key", "value"],
        [("unresolved", result.reply_unresolved),
         ("self_or_bot", result.reply_self_or_bot)],
    )
    table(
        "data_emoji",
        ["uid", "emoji", "only", "uses"],
        ((str(u), key, flag, n) for (u, key, flag), n in result.emoji_user.items()),
    )
    table(
        "data_emoji_info",
        ["emoji", "name", "animated", "external"],
        ((key, name, anim, ext) for key, (name, anim, ext) in result.emoji_info.items()),
    )
    table(
        "data_emoji_days",
        ["uid", "date", "messages"],
        ((str(u), d.isoformat(), n) for (u, d), n in result.emoji_only_day.items()),
    )

    # Реакции (схема 4). Нет листа data_reaction_meta — реакции не собирались.
    if result.reaction_mode != "off":
        def reaction_rows():
            for (u, key), n in result.reaction_recv.items():
                yield (str(u), key, "recv", n)
            for (u, key), n in result.reaction_given.items():
                yield (str(u), key, "given", n)

        table(
            "data_reaction_meta",
            ["key", "value"],
            [("mode", 2 if result.reaction_mode == "full" else 1),
             ("self_or_bot", result.reaction_self_or_bot),
             ("unattributed", result.reaction_unattributed),
             ("capped", result.reaction_capped),
             ("errors", result.reaction_errors)],
        )
        table(
            "data_reactions",
            ["uid", "emoji", "direction", "uses"],
            reaction_rows(),
        )
        table(
            "data_reaction_msgs",
            ["uid", "messages"],
            ((str(u), n) for u, n in result.reaction_msgs.items()),
        )
        table(
            "data_reaction_pairs",
            ["src", "dst", "messages"],      # «messages» = число реакций; имя колонки общее с data_replies
            ((str(a), str(b), n) for (a, b), n in result.reaction_pairs.items()),
        )
        rx_users = ({u for (u, _k) in result.reaction_given}
                    | {u for pair in result.reaction_pairs for u in pair})
        table(
            "data_reaction_users",
            ["uid", "name", "username"],
            ((str(u), agg.names.get(u, str(u)), agg.usernames.get(u, "")) for u in sorted(rx_users)),
        )

    # Журнал аудита. Нет листа data_audit_meta — аудит не собирался.
    # mode: 1 — собран, 2 — нет доступа, 3 — ошибка (данные неполные), 4 — собран не по всем серверам.
    if result.audit_mode != "off":
        table(
            "data_audit_meta",
            ["key", "value"],
            [("mode", AUDIT_MODE_CODE.get(result.audit_mode, 0)),
             ("total", result.audit_total),
             ("skipped", result.audit_skipped),
             ("unknown_actor", result.audit_unknown_actor),
             ("truncated", result.audit_truncated)],
        )
        table(
            "data_audit_actions",
            ["action", "count", "units"],
            ((code, n, result.audit_units.get(code, n)) for code, n in result.audit_actions.items()),
        )
        table(
            "data_audit_users",
            ["uid", "name", "action", "count", "units"],
            ((str(u), result.audit_names.get(u) or agg.names.get(u, str(u)), code, n,
              result.audit_user_units.get((u, code), n))
             for (u, code), n in result.audit_user_action.items()),
        )
        table(
            "data_audit_days",
            ["date", "count"],
            ((d.isoformat(), n) for d, n in sorted(result.audit_day.items())),
        )
        table(
            "data_admin_events",
            ["date", "kind", "role", "who", "who2", "server"],
            ((e.day.isoformat(), e.kind, e.role, event_person(agg, result, e.uid),
              event_person(agg, result, e.uid2), e.server) for e in result.admin_events),
        )

    if result.user_server:
        table(
            "data_servers",
            ["uid", "server", "messages"],
            ((str(u), srv, n) for (u, srv), n in result.user_server.items()),
        )
    if result.prev is not None:
        table(
            "data_prev",
            ["uid", "date", "messages"],
            ((str(u), d.isoformat(), n) for (u, d), n in result.prev.user_day.items()),
        )

def sheet_audit(wb, F, agg: Agg, result: ScanResult, params: ScanParams, name_w):
    """Лист «Аудит»: сводка по журналу аудита (действия администрации)."""
    ws = wb.add_worksheet("Аудит")
    ws.set_column(0, 0, max(name_w, 38))
    ws.set_column(1, 1, 34)
    ws.set_column(2, 5, 16)
    row = 0

    def section(text, note=None):
        nonlocal row
        ws.merge_range(row, 0, row, 5, text, F["section"])
        row += 1
        if note:
            ws.write_string(row, 0, note, F["note"])
            row += 1

    def head(titles):
        nonlocal row
        for c, t in enumerate(titles):
            ws.write_string(row, c, t, F["head"])
        row += 1

    def line(label, value):
        nonlocal row
        ws.write_string(row, 0, label, F["bold"])
        if isinstance(value, (int, float)):
            ws.write_number(row, 1, value, F["int"])
        else:
            ws.write_string(row, 1, clean(str(value)), F["text"])
        row += 1

    def actor(uid):
        if not uid:
            return AUDIT_UNKNOWN_ACTOR
        return clean(result.audit_names.get(uid) or agg.names.get(uid) or str(uid))

    total = result.audit_total
    units_total = sum(result.audit_units.values())

    # 1. Сводка
    section("1. Сводка", f"Период: {period_text(params)}. Discord хранит журнал аудита около "
                         f"{AUDIT_RETENTION_DAYS} дней.")
    line("Статус", AUDIT_STATUS.get(result.audit_mode, result.audit_mode))
    line("Записей учтено", total)
    line("Затронуто объектов (сообщений, участников и т. п.)", units_total)
    line("Типов действий", len(result.audit_actions))
    line("Участников совершили действия", len({u for (u, _c) in result.audit_user_action if u}))
    if result.audit_first:
        line("Первая запись", result.audit_first.strftime(DATE_FMT))
    if result.audit_last:
        line("Последняя запись", result.audit_last.strftime(DATE_FMT))
    line("Пропущено (автомодерация и др. из AUDIT_SKIP_ACTIONS)", result.audit_skipped)
    line("Записей без известного автора", result.audit_unknown_actor)
    if result.audit_truncated:
        line("⚠ Внимание", "Достигнут лимит числа записей: данные неполные.")
    row += 1

    if not total:
        ws.write_string(row, 0, "Записей журнала аудита за выбранный период не найдено.")
        return

    # 2. По типам действий
    section("2. По типам действий",
            "«Объектов» — сколько всего затронуто: Discord склеивает подряд идущие удаления сообщений "
            "в одну запись, поэтому сообщений обычно больше, чем записей.")
    head(["Действие", "Код", "Записей", "Доля", "Объектов"])
    by_action = sorted(result.audit_actions.items(), key=lambda it: (-it[1], it[0]))
    for code, n in by_action:
        ws.write_string(row, 0, AUDIT_LABELS.get(code, code), F["text"])
        ws.write_string(row, 1, code, F["text"])
        ws.write_number(row, 2, n, F["int"])
        ws.write_number(row, 3, n / total, F["pct"])
        ws.write_number(row, 4, result.audit_units.get(code, n), F["int"])
        row += 1
    ws.write_string(row, 0, "ВСЕГО", F["tot_text"])
    ws.write_blank(row, 1, None, F["tot_text"])
    ws.write_number(row, 2, total, F["tot_int"])
    ws.write_blank(row, 3, None, F["tot_text"])
    ws.write_number(row, 4, units_total, F["tot_int"])
    row += 2

    # 3. По дням
    section("3. По дням", "Дни — по часовому поясу " + TZ_NAME + ". Дни без записей не показаны.")
    head(["Дата", "Записей"])
    for day, n in sorted(result.audit_day.items()):
        ws.write_datetime(row, 0, day, F["date"])
        ws.write_number(row, 1, n, F["int"])
        row += 1
    row += 1

    # Подготовка данных по участникам
    a_total: dict = defaultdict(int)
    a_units: dict = defaultdict(int)
    per_actor: dict = defaultdict(list)
    for (uid, code), n in result.audit_user_action.items():
        a_total[uid] += n
        per_actor[uid].append((code, n, result.audit_user_units.get((uid, code), n)))
    for (uid, _code), u in result.audit_user_units.items():
        a_units[uid] += u
    ranked = sorted(a_total, key=lambda u: (-a_total[u], actor(u).casefold(), u))

    # 4. Кто сколько действий совершил
    section("4. Кто сколько действий совершил")
    head(["Пользователь", "ID", "Записей", "Доля", "Объектов", "Типов действий"])
    for uid in ranked:
        ws.write_string(row, 0, actor(uid), F["text"])
        ws.write_string(row, 1, str(uid) if uid else "—", F["text"])
        ws.write_number(row, 2, a_total[uid], F["int"])
        ws.write_number(row, 3, a_total[uid] / total, F["pct"])
        ws.write_number(row, 4, a_units[uid], F["int"])
        ws.write_number(row, 5, len(per_actor[uid]), F["int"])
        row += 1
    row += 1

    # 5. Кто что сделал
    section("5. Кто что сделал", "Участники — по убыванию числа записей, внутри — по убыванию действий.")
    head(["Пользователь", "Действие", "Записей", "Объектов"])
    for uid in ranked:
        for code, n, units in sorted(per_actor[uid], key=lambda it: (-it[1], it[0])):
            ws.write_string(row, 0, actor(uid), F["text"])
            ws.write_string(row, 1, AUDIT_LABELS.get(code, code), F["text"])
            ws.write_number(row, 2, n, F["int"])
            ws.write_number(row, 3, units, F["int"])
            row += 1

def sheet_info(wb, F, guild_name, params, state, matrix_note, truncated, merged=False):
    wi = wb.add_worksheet("Служебное")
    wi.set_column(0, 0, 40)
    wi.set_column(1, 1, 90)
    info = [
        ("Сервер", guild_name),
        ("Период", period_text(params)),
        ("Схема данных", "4"),
        ("Период: начало (ISO)", params.start_date.isoformat() if params.start_date else "нет"),
        ("Период: конец (ISO)", params.end_date.isoformat() if params.end_date else "нет"),
        ("Суммарный файл", "да" if merged else "нет"),
    ]
    if params.compare:
        info.append(("Сравнение с периодом", prev_period_text(params)))
    info += [
        ("Часовой пояс", TZ_NAME),
        ("Фильтр: пользователь", params.user.display_name if params.user else "все"),
        ("Фильтр: канал", chan_label(params.channel) if params.channel else "все"),
        ("Порядок пользователей", SORT_LABELS[params.sort_key]),
        ("Матрица", matrix_note),
        ("Сформирован", datetime.now(TZ).strftime("%d.%m.%Y %H:%M")),
        ("Каналов и веток обработано", f"{state.channels_done} из {state.total_channels}"),
        ("Не читались (в периоде там нет сообщений)", fmt_num(state.inactive)),
        ("Просмотрено сообщений", fmt_num(state.seen)),
        ("Учтено сообщений", fmt_num(state.counted)),
        ("Что считается", "Обычные сообщения и ответы людей; боты и системные сообщения не учитываются."),
        ("Реакции", REACTION_LABELS.get(params.reactions, params.reactions)),
    ]
    if params.reactions != "off":
        info.append(("Сообщений с реакциями", fmt_num(state.reaction_msgs)))
        if params.reactions == "full":
            info.append(("Запросов «кто поставил реакцию»", fmt_num(state.reaction_requests)))
    if params.compare:
        info.append(("Про просмотренные", "Счётчики сообщений включают и предыдущий период (его тоже приходится читать)."))
    if state.cancelled:
        info.append(("⚠ Внимание", "Сбор остановлен вручную: данные НЕПОЛНЫЕ."))
    if truncated:
        info.append(("⚠ Внимание", f"Лист «По дням (список)» обрезан до {fmt_num(MAX_LIST_ROWS)} строк (лимит Excel)."))
    for note in state.notes:
        info.append(("Примечание", note))
    row = 0
    for k, v in info:
        wi.write_string(row, 0, k, F["bold"])
        wi.write_string(row, 1, clean(v), F["wrap"])
        row += 1

    def section(title: str, items: list[str]):
        nonlocal row
        if not items:
            return
        row += 1
        wi.write_string(row, 0, f"{title} ({len(items)})", F["section"])
        wi.write_blank(row, 1, None, F["section"])
        for it in items:
            row += 1
            wi.write_string(row, 1, clean(it))
        row += 1

    section("Исключено по правилам (вместе с ветками)", state.excluded)
    section("Правила исключения, которые ни с чем не совпали", state.unused_rules)
    section("Нет доступа", state.skipped)
    section("Ошибки чтения", state.failed)

def build_xlsx(guild_name: str, params: ScanParams, state: ScanState, result: ScanResult) -> bytes:
    agg = aggregate(params, result)
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {
        "in_memory": True,
        "strings_to_numbers": False,
        "strings_to_formulas": False,
        "strings_to_urls": False,
    })
    F = make_formats(wb)
    name_w = min(45, max(14, max((len(agg.name_of(u)) for u in agg.order), default=14) + 2))

    sheet_summary(wb, F, agg, params, state, name_w)
    matrix_note = sheet_matrix(wb, F, agg, params, name_w)
    truncated = sheet_daily(wb, F, agg, name_w)
    sheet_analytics(wb, F, agg, result, params, name_w)
    sheet_staff(wb, F, agg, result, guild_name, name_w)
    if result.audit_mode != "off":
        sheet_audit(wb, F, agg, result, params, name_w)
    sheet_info(wb, F, guild_name, params, state, matrix_note, truncated, agg.merged)
    sheet_data(wb, agg, result)

    wb.close()
    return buf.getvalue()

# ---------- Текстовый отчёт ----------

def build_report(guild_name: str, params: ScanParams, state: ScanState, result: ScanResult) -> str:
    agg = aggregate(params, result)
    order, totals, per_user = agg.order, agg.totals, agg.per_user
    name_of = agg.name_of

    lines = [
        "=== ОТЧЕТ ПО СООБЩЕНИЯМ СЕРВЕРА ===",
        f"Сервер: {guild_name}",
        f"Период: {period_text(params)} (часовой пояс: {TZ_NAME})",
        f"Фильтры: пользователь = {params.user.display_name if params.user else 'все'}, "
        f"канал = {chan_label(params.channel) if params.channel else 'все'}",
        f"Порядок пользователей: {SORT_LABELS[params.sort_key]}; даты — по возрастанию",
        f"Сформирован: {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}",
        f"Обработано каналов и веток: {state.channels_done} из {state.total_channels}; "
        f"просмотрено сообщений: {fmt_num(state.seen)}; учтено: {fmt_num(state.counted)}",
        "Учитываются обычные сообщения и ответы людей (боты и системные сообщения не считаются).",
    ]
    if state.cancelled:
        lines.append("!!! Сбор остановлен вручную: данные НЕПОЛНЫЕ.")
    for note in state.notes:
        lines.append(f"Примечание: {note}")
    if state.excluded:
        lines.append(f"Исключено по правилам, вместе с ветками ({len(state.excluded)}): {fmt_list(state.excluded)}")
    if state.unused_rules:
        lines.append(f"Правила исключения без совпадений ({len(state.unused_rules)}): {fmt_list(state.unused_rules)}")
    if state.inactive:
        lines.append(f"Не читались (в периоде там нет сообщений): {state.inactive}")
    if state.skipped:
        lines.append(f"Нет доступа к ({len(state.skipped)}): {fmt_list(state.skipped)}")
    if state.failed:
        lines.append(f"Ошибка чтения ({len(state.failed)}): {fmt_list(state.failed)}")
    lines.append("")

    for srv, info in staff_blocks(result, guild_name):
        rows, total = staff_rows(agg, result, srv, info)
        lines.append(f"--- АДМИНИСТРАЦИЯ: {srv} ---")
        staff_sum = 0
        for uid, mb, n in rows:
            staff_sum += n
            roles = f" ({', '.join(mb.roles)})" if mb.roles else ""
            lines.append(f"  {KIND_RU[mb.kind]}: {staff_label(mb)} (ID {uid}){roles} — сообщений: {n}")
        share = f"{staff_sum / total * 100:.1f}%" if total else "0%"
        lines.append(f"  Всего: {len(rows)} чел., сообщений: {staff_sum} ({share} от всех)")
        if info.unmatched_roles:
            lines.append(f"  Не найдены роли из ADMIN_ROLES: {', '.join(info.unmatched_roles)}")
        lines.append("")

    if not order:
        lines.append("Сообщений за выбранный период не найдено.")
        return "\n".join(lines) + "\n"

    lines.append("--- ИТОГО ПО ПОЛЬЗОВАТЕЛЯМ ---")
    lines.append(f"{'№':>4}  {'Пользователь':<{NAME_WIDTH}}  {'ID':<20}  {'Сообщений':>10}  {'Дней':>5}")
    lines.append("-" * (4 + 2 + NAME_WIDTH + 2 + 20 + 2 + 10 + 2 + 5))
    for i, uid in enumerate(order, start=1):
        lines.append(
            f"{i:>4}  {fit_name(name_of(uid)):<{NAME_WIDTH}}  {uid:<20}  "
            f"{totals[uid]:>10}  {len(per_user[uid]):>5}"
        )
    lines.append(f"{'':>4}  {'ВСЕГО':<{NAME_WIDTH}}  {'':<20}  {sum(totals.values()):>10}")
    lines.append("")

    lines.append("--- ПОДРОБНО ПО ДНЯМ ---")
    for uid in order:
        lines.append("")
        lines.append(f"{name_of(uid)} (ID {uid}) — всего {totals[uid]}, дней с сообщениями: {len(per_user[uid])}")
        for day, n in sorted(per_user[uid].items()):
            lines.append(f"    {day.strftime(DATE_FMT)} | {n}")

    return "\n".join(lines) + "\n"