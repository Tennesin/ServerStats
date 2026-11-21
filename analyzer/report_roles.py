"""Этап 6: роли, боты, присутствие для HTML-отчётов. Раздел показывается, только если в базе есть снимок."""

import botroot  # noqa: F401
import numpy as np
from config import PERMISSION_LABELS

from report_html import Raw, _bad, card, esc, h2, table

STYLE = """<style>
.swatch{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;
vertical-align:middle;border:1px solid var(--line)}
.chip{display:inline-block;padding:1px 9px;margin:2px 4px 2px 0;border:1px solid var(--line);
border-radius:10px;font-size:12px;background:var(--card)}
</style>"""

KEY_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members",
             "kick_members", "moderate_members", "manage_messages", "manage_webhooks",
             "mention_everyone", "view_audit_log")
ADMIN_PERMS = ("administrator", "manage_guild", "manage_roles")     # право, делающее человека «администратором по факту»

def _perm_text(perms) -> str:
    if "administrator" in perms:
        return "Администратор (все права)"
    got = [PERMISSION_LABELS.get(p, p) for p in KEY_PERMS if p in perms]
    return ", ".join(got) or "—"

def _color(value) -> Raw:
    v = int(value)
    if not v:
        return Raw('<span class="muted">не задан</span>')
    return Raw(f'<span class="swatch" style="background:#{v:06X}"></span>#{v:06X}')

def _flags(r) -> str:
    bits = []
    if r.hoist:
        bits.append("отдельной группой")
    if r.mentionable:
        bits.append("можно упоминать")
    if r.managed:
        bits.append("бот / интеграция")
    return ", ".join(bits) or "—"

def build_server_roles_html(ds) -> str:
    if not getattr(ds, "has_roles", False) or ds.role_table.empty:
        return ""
    rt, mr = ds.role_table, ds.member_roles
    staff = {(r.server, r.uid) for r in ds.staff.itertuples()}
    servers = list(rt["server"].unique())
    multi = len(servers) > 1

    b = [STYLE, h2("Роли и боты")]
    snaps = [si.snapshot_at for si in ds.sync_info.values() if si.snapshot_at]
    note = "Снимок ролей и участников сделан при последней синхронизации"
    if snaps:
        note += f" ({min(snaps):%d.%m.%Y %H:%M})"
    b.append(f'<p class="muted">{esc(note)}. Роль @everyone не показана.</p>')

    for server in servers:
        roles = (rt[(rt["server"] == server) & (rt["is_everyone"] == 0)]
                 .sort_values("position", ascending=False, kind="stable"))
        people = mr[(mr["server"] == server) & (mr["is_bot"] == 0)]
        bots = mr[(mr["server"] == server) & (mr["is_bot"] == 1)]
        if multi:
            b.append(f"<h3>{esc(server)}</h3>")

        no_roles = sum(1 for x in people["roles"] if not x)
        b.append('<div class="cards">' + "".join([
            card("Ролей", len(roles)),
            card("Людей", len(people), f"без ролей: {no_roles}"),
            card("Ботов", len(bots)),
        ]) + "</div>")

        rows = [[r.name, _color(r.color), int(r.members), _flags(r), _perm_text(r.perms)]
                for r in roles.itertuples()]
        b.append(table(["Роль", "Цвет", "Участников (с ботами)", "Особенности", "Ключевые права"],
                       rows, num_cols=(2,)))

        if len(bots):
            b.append("<h4>Боты на сервере</h4>")
            b.append('<p class="muted">Их сообщения, реакции и ответы в статистику не входят.</p>')
            rows = [[m.name, m.username, ", ".join(m.roles) or "—"]
                    for m in bots.sort_values("name", key=lambda s: s.str.casefold()).itertuples()]
            b.append(table(["Бот", "Логин", "Роли"], rows))

        # Люди с административными правами, которых нет в списке администрации (ADMIN_ROLES)
        perm_of: dict = {}
        for r in roles.itertuples():
            perm_of.setdefault(r.name, r.perms)
        rows = []
        for m in people.itertuples():
            if (server, m.uid) in staff:
                continue
            hit = [n for n in m.roles if any(p in perm_of.get(n, ()) for p in ADMIN_PERMS)]
            if hit:
                rows.append([m.name, ", ".join(hit), _perm_text(set().union(*(perm_of[n] for n in hit)))])
        if rows:
            b.append("<h4>Административные права вне списка администрации</h4>")
            b.append('<p class="muted">У этих людей есть роль с правом «Администратор», «Управлять сервером» '
                     "или «Управлять ролями», но такой роли нет в ADMIN_ROLES (config.py бота). "
                     "Поэтому в «Администрации» и в компоненте G они учтены без роли. Либо роль нужно "
                     "добавить в ADMIN_ROLES, либо права выданы лишние.</p>")
            b.append(table(["Пользователь", "Роли с такими правами", "Ключевые права"], rows))
    return "".join(b)

def build_user_status_html(ds, uid, um) -> str:
    """Личный блок: на сервере ли человек и какие у него роли. Нет снимка — пусто."""
    if uid not in um.index or "in_server" not in um.columns:
        return ""
    r = um.loc[uid]
    st = r["in_server"]
    state = bool(st) if isinstance(st, (bool, np.bool_)) else None
    cards = []
    if state is True:
        j = r["joined"]
        cards.append(card("На сервере", "да", "" if _bad(j) else f"вступил(а): {j:%d.%m.%Y}"))
    elif state is False:
        left = r["left_date"]
        cards.append(card("На сервере", "нет", "дата ухода неизвестна" if _bad(left) else f"ушёл(ла) ≈ {left:%d.%m.%Y}"))

    chips = ""
    if getattr(ds, "has_roles", False) and len(ds.member_roles):
        mine = ds.member_roles[ds.member_roles["uid"] == uid]
        multi = ds.member_roles["server"].nunique() > 1
        parts = []
        for m in mine.itertuples():
            if m.roles:
                head = f"<b>{esc(m.server)}:</b> " if multi else ""
                parts.append("<p>" + head + "".join(f'<span class="chip">{esc(n)}</span>' for n in m.roles) + "</p>")
        chips = "".join(parts)
        if mine.shape[0]:
            cards.append(card("Ролей", sum(len(m.roles) for m in mine.itertuples())))

    if not cards and not chips:
        return ""
    out = [STYLE, h2("Присутствие и роли", open=True)]
    if cards:
        out.append('<div class="cards">' + "".join(cards) + "</div>")
    if state is False:
        out.append('<div class="box warn">Человека нет на сервере (по последнему снимку). '
                   "Цифры ниже показывают его вклад за период.</div>")
    if chips:
        out.append(chips)
    return "".join(out)