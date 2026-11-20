"""Этап 6: журнал аудита для HTML-отчётов. Серверный раздел и личный блок (только у тех, у кого есть действия)."""

import botroot  # noqa: F401  (подключает корень бота, чтобы импортировать config)
from config import AUDIT_LABELS

import charts as C
import metrics as M
import settings as S
from metrics import calendar_days, make_rel
from report_html import _bad, _link, card, esc, h2, img, num, pct, table
from scoring import audit_weight

def _label(code: str) -> str:
    return AUDIT_LABELS.get(code, code)

def _rel(ds):
    level = M._lvl(ds.audit_total, 20, 200)
    notes = [
        f"Discord хранит журнал около {S.AUDIT_RETENTION_DAYS} дн.; бот копит записи в своей базе, "
        "поэтому глубина зависит от того, когда начался сбор",
        "«Объектов» — сколько всего затронуто: Discord склеивает подряд идущие удаления сообщений в одну запись",
        "автомодерация и действия из AUDIT_SKIP_ACTIONS не учитываются",
    ]
    since = [si.audit_since for si in ds.sync_info.values() if si.audit_since]
    if since:
        notes.append(f"в базе записи копятся с {min(since):%d.%m.%Y}")
    if ds.audit_mode in (3, 4):
        level = min(level, 1)
        notes.append("журнал собран с ошибкой или не по всем серверам: данные неполные")
    return make_rel(level, notes)

def build_server_audit_html(ds, linked) -> str:
    if getattr(ds, "audit_mode", 0) == 0:
        return ""
    if ds.audit_mode == 2:
        return (h2("Журнал аудита")
                + '<div class="box">Журнал аудита недоступен: боту нужно право «Просмотр журнала аудита».</div>')
    rel = _rel(ds)
    b = [h2("Журнал аудита", rel)]
    if ds.audit_total == 0:
        b.append('<div class="box">Записей журнала аудита за период не найдено.</div>')
        return "".join(b)

    actors = int(ds.audit_users["uid"].nunique()) if len(ds.audit_users) else 0
    units = int(ds.audit_actions["units"].sum())
    b.append('<div class="cards">' + "".join([
        card("Записей", num(ds.audit_total), f"типов действий: {len(ds.audit_actions)}"),
        card("Затронуто объектов", num(units), "сообщений, участников и т. п."),
        card("Людей совершали действия", num(actors), f"записей без автора: {ds.audit_unknown_actor}"),
        card("Пропущено", num(ds.audit_skipped), "автомодерация и т. п."),
    ]) + "</div>")
    b.append(img(C.chart_audit_days(ds.audit_days, calendar_days(ds)), "Журнал аудита по дням"))

    b.append("<h3>Какие действия совершались</h3>")
    acts = ds.audit_actions.sort_values(["count", "action"], ascending=[False, True])
    rows = [[_label(r.action), r.action, int(r.count), pct(r.count / ds.audit_total), int(r.units),
             f"{audit_weight(r.action):g}"] for r in acts.head(20).itertuples()]
    b.append(table(["Действие", "Код", "Записей", "Доля", "Объектов", "Вес в СЗП"], rows,
                   num_cols=(2, 3, 4, 5)))
    if len(acts) > 20:
        b.append(f'<p class="muted">Показаны 20 из {len(acts)} типов действий.</p>')

    if len(ds.audit_users):
        b.append("<h3>Кто сколько действий совершил</h3>")
        au = ds.audit_users.copy()
        au["w"] = au["count"] * au["action"].map(audit_weight)
        g = (au.groupby("uid").agg(name=("name", "first"), count=("count", "sum"), units=("units", "sum"),
                                   kinds=("action", "nunique"), w=("w", "sum"))
               .sort_values(["count", "w"], ascending=[False, False]))
        main = (au.sort_values(["uid", "count"], ascending=[True, False])
                  .drop_duplicates("uid").set_index("uid")["action"])
        rows = [[i, _link(uid, r["name"], linked), int(r["count"]), int(r["units"]), int(r["kinds"]),
                 _label(main[uid]), f"{r['w']:.1f}"]
                for i, (uid, r) in enumerate(g.head(15).iterrows(), start=1)]
        b.append(table(["№", "Пользователь", "Записей", "Объектов", "Типов", "Главное действие", "Взвешенно"],
                       rows, num_cols=(0, 2, 3, 4, 6)))
        if len(g) > 15:
            b.append(f'<p class="muted">Показаны 15 из {len(g)} человек.</p>')

    b.append('<div class="box muted"><b>Как читать:</b><ul>'
             + "".join(f"<li>{esc(x)}</li>" for x in rel["notes"])
             + "<li>Ключевые события администрации (выдача и снятие ролей) — в разделе «Администрация».</li>"
             "</ul></div>")
    return "".join(b)

def build_user_audit_html(ds, uid, sk) -> str:
    """Личный блок: показывается, только если у человека есть действия в журнале."""
    if not getattr(ds, "has_audit", False) or ds.audit_users.empty:
        return ""
    mine = ds.audit_users[ds.audit_users["uid"] == uid]
    if mine.empty:
        return ""
    total = int(mine["count"].sum())
    units = int(mine["units"].sum())
    cards = [
        card("Записей в журнале", num(total), f"{pct(total / ds.audit_total)} от всех" if ds.audit_total else ""),
        card("Затронуто объектов", num(units)),
        card("Типов действий", len(mine)),
    ]
    if uid in sk.index and not _bad(sk.loc[uid, "g_audit"]):
        cards.append(card("Взвешенно (для G)", num(sk.loc[uid, "g_audit"], 1), "вес типов — в настройках"))
    rows = []
    for r in mine.sort_values(["count", "action"], ascending=[False, True]).itertuples():
        w = audit_weight(r.action)
        rows.append([_label(r.action), int(r.count), int(r.units), f"{w:g}", f"{r.count * w:.1f}"])
    return (h2("Работа по журналу аудита")
            + '<div class="cards">' + "".join(cards) + "</div>"
            + table(["Действие", "Записей", "Объектов", "Вес", "Взвешенно"], rows, num_cols=(1, 2, 3, 4))
            + '<p class="muted">Журнал показывает действия администрации: модерацию, настройки, роли. '
              "Веса типов задаются в SZP_AUDIT_WEIGHTS (analyzer/settings.py).</p>")