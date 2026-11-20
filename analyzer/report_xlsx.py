"""Этап 5: сводный xlsx по результатам анализа."""

import math
import re
from datetime import date, datetime
import numpy as np
import pandas as pd
import xlsxwriter
import settings as S

from emoji_tools import info_map, label_text

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def _put(ws, r, c, v, fmt=None):
    if v is None or v is pd.NaT:
        ws.write_blank(r, c, None, fmt)
    elif isinstance(v, (bool, np.bool_)):
        ws.write_string(r, c, "да" if v else "нет", fmt)
    elif isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        if math.isfinite(f):
            ws.write_number(r, c, f, fmt)
        else:
            ws.write_blank(r, c, None, fmt)
    elif isinstance(v, (date, datetime)):
        ws.write_string(r, c, v.strftime("%d.%m.%Y"), fmt)
    else:
        ws.write_string(r, c, _CTRL.sub("", str(v)), fmt)

def _sheet_name(base: str, used: set) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "_", base).strip("'")[:31] or "Лист"
    cand, i = name, 2
    while cand.lower() in used:
        suffix = f" ({i})"
        cand = name[: 31 - len(suffix)] + suffix
        i += 1
    used.add(cand.lower())
    return cand

SERVER_HEADERS = ["Сервер", "Суммарный", "Период", "Дней в окне", "Пользователей", "Сообщений",
                  "В среднем в день", "DAU (среднее)", "Джини", "Доля топ-5", "Тренд",
                  "Удержание 1→2 половина", "Достоверность", "Предупреждений"]
SERVER_FMTS = ["text", "text", "text", "int", "int", "int", "dec1", "dec1", "dec", "pct", "text",
               "pct", "text", "int"]

USER_COLUMNS = [
    ("№ по СЗП", "szp_rank", "int"), ("Пользователь", "name", "text"), ("Логин", "username", "text"),
    ("ID", "__uid", "text"), ("СЗП", "szp", "dec1"), ("Уровень СЗП", "szp_level", "text"),
    ("Достоверность", "reliability", "text"), ("Сообщений", "messages", "int"),
    ("Место по сообщениям", "rank", "int"), ("Активных дней", "active_days", "int"),
    ("Дней в окне", "d_total", "int"),
    ("Доля активных дней", "active_share", "pct"), ("Недель активно", "weeks_active", "int"),
    ("Среднее в активный день", "avg_per_active_day", "dec"), ("Макс. за день", "max_day", "int"),
    ("Лучшая серия", "longest_streak", "int"), ("Текущая серия", "current_streak", "int"),
    ("Дней с последнего сообщения", "days_since_last", "int"), ("Тренд", "trend", "text"),
    ("Ответов", "replies", "int"), ("Доля ответов", "reply_share", "pct"),
    ("Каналов", "channels_used", "int"), ("Главный канал", "top_channel", "text"),
    ("V объём", "V", "dec"), ("C постоянство", "C", "dec"), ("O отклик", "O", "dec"),
    ("G управление", "G", "dec"), ("B охват", "B", "dec"),
    ("Обращений к нему", "appeals_in", "int"), ("Действий аудита (взвеш.)", "g_audit", "dec1"),
    ("Роль", "role", "text"),
    ("Эмодзи всего", "emoji_uses", "int"), ("Эмодзи на сообщение", "emoji_per_msg", "dec"),
    ("Сообщений «только эмодзи»", "emoji_only_msgs", "int"),
    ("Доля «только эмодзи»", "emoji_only_share", "pct"),
    ("Сообщений с текстом", "content_msgs", "int"), ("Активных дней с текстом", "content_days", "int"),
    ("Любимый эмодзи", "emoji_top_label", "text"),
    ("Реакций получено", "rx_recv", "int"), ("Сообщений с реакциями", "rx_msgs", "int"),
    ("Доля сообщений с реакциями", "rx_msg_share", "pct"), ("Реакций на сообщение", "rx_per_msg", "dec"),
    ("Любимая полученная реакция", "rx_top_label", "text"),
    ("Реакций поставлено", "rx_given", "int"), ("Ставил(а) реакции людям", "rx_given_targets", "int"),
    ("Любимая поставленная реакция", "rx_given_top_label", "text"),
]

METHOD_LINES = [
    "СЗП — степень значимости пользователя для сервера, 0–100: насколько изменится жизнь сервера, если человек пропадёт.",
    "Сумма пяти компонентов (0–1) с весами:",
    f"V объём ({S.SZP_WEIGHTS['V']:.0%}): √(доля человека в сообщениях сервера ÷ доля лидера).",
    f"C постоянство ({S.SZP_WEIGHTS['C']:.0%}): {S.SZP_C_ALL_W} × (активных дней ÷ дней от начала сервера) + {1 - S.SZP_C_ALL_W:.1f} × (активных дней из последних {S.SZP_RECENT_DAYS} ÷ {S.SZP_RECENT_DAYS}). Считается, только если с начала сервера прошло ≥{S.SZP_MIN_DAYS} дн.",
    f"O отклик ({S.SZP_WEIGHTS['O']:.0%}): {S.SZP_O_APPEAL_W:.0%} входящие обращения (ответы и теги, с учётом числа разных людей), остальное — полученные реакции; оба как √(значение ÷ максимум). При <{S.SZP_O_MIN_MSGS} сообщений снижается.",
    f"G управление ({S.SZP_WEIGHTS['G']:.0%}): {S.SZP_G_ROLE_W:.0%} роль (основатель {S.SZP_ROLE['owner']}, админ {S.SZP_ROLE['admin']}, неформальная — вручную), остальное — работа по журналу аудита: ln(1+взвешенные действия) относительно лидера.",
    f"B охват ({S.SZP_WEIGHTS['B']:.0%}): значимых каналов (≥{S.SZP_SIG_CHANNEL:.0%} сообщений сервера), где ≥{S.SZP_B_MIN_MSGS} сообщений, ÷ число значимых каналов.",
    "В суммарном файле дни считаются от самой ранней даты начала серверов, роль берётся лучшая, действия аудита суммируются.",
    "Динамика в СЗП отдельно не входит. Если компонент недоступен (старый формат, молодой сервер, нет журнала аудита), его вес перераспределяется.",
    "",
    "Веса и пороги заданы экспертно и сверены только с ручной оценкой на малых серверах.",
    "СЗП сравнивает людей внутри одного сервера за период; это не оценка человека и не основание для санкций.",
    "Достоверность (низкая/средняя/высокая) зависит от длины окна, числа сообщений и людей; "
    "при короткой истории все выводы ориентировочные.",
    "Реакции собраны в режиме «счётчики» (в них входят боты и самореакции), журнал аудита Discord хранит около 45 дней.",
    "",
    "Эмодзи: считаются во всех сообщениях; «только эмодзи» — сообщение без другого текста. "
    "Оттенки и вариации одного эмодзи объединены. «Внешний» — кастомный эмодзи не из списка эмодзи сервера.",
    "Дни с текстом — активные дни, когда было хотя бы одно сообщение не только из эмодзи. В СЗП эмодзи не входят.",
]

def _server_row(it) -> list:
    ds, sm = it["ds"], it["sm"]
    base = [ds.title, "да" if ds.is_merged else "нет", ds.period_text, ds.window_days]
    level = sm["reliability"]["level"]
    if sm["empty"]:
        return base + [0, 0] + [None] * 6 + [level, len(ds.warnings)]
    ov, d, tr, lc = sm["overview"], sm["distribution"], sm["trend"], sm["lifecycle"]
    return base + [sm["users"], sm["messages"], ov["avg_per_day"], ov["avg_dau"], d["gini"], d["top5"],
                   tr["direction"] if tr["available"] else "не оценивается",
                   lc.get("retention"), level, len(ds.warnings)]

def build_summary_xlsx(items: list, path):
    wb = xlsxwriter.Workbook(str(path), {"strings_to_numbers": False, "strings_to_formulas": False,
                                         "strings_to_urls": False})
    border = {"border": 1, "border_color": "#D9D9D9", "valign": "vcenter"}
    F = {
        "head": wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1,
                               "align": "center", "valign": "vcenter", "text_wrap": True}),
        "text": wb.add_format(border),
        "int": wb.add_format({**border, "num_format": "#,##0"}),
        "dec": wb.add_format({**border, "num_format": "0.00"}),
        "dec1": wb.add_format({**border, "num_format": "0.0"}),
        "pct": wb.add_format({**border, "num_format": "0.0%"}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
    }
    used: set = set()

    # Серверы
    ws = wb.add_worksheet(_sheet_name("Серверы", used))
    for c, h in enumerate(SERVER_HEADERS):
        ws.write_string(0, c, h, F["head"])
    ws.set_row(0, 34)
    ws.set_column(0, 0, 34)
    ws.set_column(1, 1, 11)
    ws.set_column(2, 2, 38)
    ws.set_column(3, len(SERVER_HEADERS) - 1, 14)
    for r, it in enumerate(items, start=1):
        for c, (v, f) in enumerate(zip(_server_row(it), SERVER_FMTS)):
            _put(ws, r, c, v, F[f])
    ws.freeze_panes(1, 1)

    # Пользователи
    for it in items:
        um, sk, ds = it["um"], it["sk"], it["ds"]
        if um is None or um.empty or sk is None or sk.empty:
            continue
        df = um.join(sk).sort_values("szp", ascending=False, kind="stable")
        df["__uid"] = df.index.astype(str)
        ws = wb.add_worksheet(_sheet_name(f"Польз. {ds.title}", used))
        for c, (h, _k, _f) in enumerate(USER_COLUMNS):
            ws.write_string(0, c, h, F["head"])
        ws.set_row(0, 46)
        for r, (_uid, row) in enumerate(df.iterrows(), start=1):
            for c, (_h, key, f) in enumerate(USER_COLUMNS):
                _put(ws, r, c, row[key], F[f])
        wide = {"top_channel", "role", "emoji_top_label", "rx_top_label", "rx_given_top_label"}
        for c, (_h, key, _f) in enumerate(USER_COLUMNS):
            if key in wide:
                width = 30
            elif key in ("name", "username"):
                width = 24
            elif key == "__uid":
                width = 21
            elif key == "szp_rank":
                width = 9
            else:
                width = 13
            ws.set_column(c, c, width)
        ws.autofilter(0, 0, len(df), len(USER_COLUMNS) - 1)
        ws.freeze_panes(1, 2)

    # Эмодзи (по серверу)
    for it in items:
        em, ds = it.get("em"), it["ds"]
        if not em or not em.get("available") or em.get("empty") or em.get("table") is None:
            continue
        info = info_map(ds.emoji_info)
        ws = wb.add_worksheet(_sheet_name(f"Эмодзи {ds.title}", used))
        heads = ["№", "Эмодзи", "Тип", "Использований", "Доля", "Людей", "Из них в «только эмодзи»"]
        for c, h in enumerate(heads):
            ws.write_string(0, c, h, F["head"])
        ws.set_row(0, 34)
        for r, (key, x) in enumerate(em["table"].head(300).iterrows(), start=1):
            _put(ws, r, 0, r, F["int"])
            _put(ws, r, 1, label_text(key, info), F["text"])
            _put(ws, r, 2, "кастомный" if x["kind"] == "custom" else "Unicode", F["text"])
            _put(ws, r, 3, int(x["uses"]), F["int"])
            _put(ws, r, 4, float(x["share"]), F["pct"])
            _put(ws, r, 5, int(x["users"]), F["int"])
            _put(ws, r, 6, int(x["only_uses"]), F["int"])
        ws.set_column(0, 0, 6)
        ws.set_column(1, 1, 34)
        ws.set_column(2, 2, 12)
        ws.set_column(3, 6, 15)
        ws.autofilter(0, 0, min(len(em["table"]), 300), len(heads) - 1)
        ws.freeze_panes(1, 2)

    # Реакции (по серверу)
    for it in items:
        rx, ds = it.get("rx"), it["ds"]
        if not rx or not rx.get("available") or rx.get("empty") or rx.get("table") is None:
            continue
        info = info_map(ds.emoji_info)
        full = rx["full"]
        ws = wb.add_worksheet(_sheet_name(f"Реакции {ds.title}", used))
        heads = ["№", "Эмодзи", "Тип", "Реакций", "Доля", "Получателей", "В тексте сообщений"]
        if full:
            heads.append("Поставили (людей)")
        for c, h in enumerate(heads):
            ws.write_string(0, c, h, F["head"])
        ws.set_row(0, 34)
        shown = rx["table"].head(300)
        for r, (key, x) in enumerate(shown.iterrows(), start=1):
            _put(ws, r, 0, r, F["int"])
            _put(ws, r, 1, label_text(key, info), F["text"])
            _put(ws, r, 2, "кастомный" if x["kind"] == "custom" else "Unicode", F["text"])
            _put(ws, r, 3, int(x["uses"]), F["int"])
            _put(ws, r, 4, float(x["share"]), F["pct"])
            _put(ws, r, 5, int(x["users"]), F["int"])
            _put(ws, r, 6, int(x["text_uses"]), F["int"])
            if full:
                _put(ws, r, 7, int(x["givers"]) if "givers" in x.index else None, F["int"])
        ws.set_column(0, 0, 6)
        ws.set_column(1, 1, 34)
        ws.set_column(2, 2, 12)
        ws.set_column(3, 7, 16)
        ws.autofilter(0, 0, len(shown), len(heads) - 1)
        ws.freeze_panes(1, 2)
        note_row = len(shown) + 2
        ws.write_string(note_row, 1, "Как читать:", F["wrap"])
        for k, line in enumerate(rx["rel"]["notes"], start=1):
            ws.write_string(note_row + k, 1, "• " + line)

    # Методика
    ws = wb.add_worksheet(_sheet_name("Методика", used))
    ws.set_column(0, 0, 130)
    for r, line in enumerate(METHOD_LINES):
        ws.write_string(r, 0, line, F["wrap"])

    wb.close()