"""Этап 5: HTML-отчёты (один файл, картинки вшиты в base64)."""

import html
import json
from datetime import datetime, timedelta
from urllib.parse import quote

import numpy as np

import charts as C
import metrics as M
import settings as S

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2430;--muted:#6b7380;--line:#e3e6eb;--accent:#1F4E78;
--row-alt:#fafbfc;--warn-bg:#fff8e5;--track:#eef0f3}
:root[data-theme="dark"]{--bg:#12161c;--card:#1b212b;--ink:#e3e7ed;--muted:#97a1af;--line:#2c3441;
--accent:#4a8fd0;--row-alt:#202733;--warn-bg:#3a3118;--track:#2a323f;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:24px 16px 60px}
h1{margin:0 0 4px;font-size:26px}
h2{margin:32px 0 10px;font-size:19px;color:var(--accent);border-bottom:2px solid var(--line);padding-bottom:6px}
.muted{color:var(--muted);font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card .v{font-size:22px;font-weight:700}
.card .l{font-size:12px;color:var(--muted)}
.card .h{font-size:12px;color:var(--muted)}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.warn{border-color:#e0b252;background:var(--warn-bg)}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:14px;margin:8px 0}
th{background:#1F4E78;color:#fff;text-align:left;padding:6px 8px;font-weight:600}
td{border-bottom:1px solid var(--line);padding:5px 8px;vertical-align:top}
tr:nth-child(even) td{background:var(--row-alt)}
td.n,th.n{text-align:right}
.tblwrap{overflow-x:auto}
img{max-width:100%;height:auto;display:block;margin:10px auto;border:1px solid var(--line);border-radius:8px;background:#fff}
:root[data-theme="dark"] img.chart{filter:invert(.92) hue-rotate(180deg)}
.badge{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px;font-weight:600;color:#fff}
.b-hi{background:#2E9E5B}.b-mid{background:#d89b1d}.b-lo{background:#c9443f}
a{color:var(--accent)}
ul{margin:6px 0 6px 20px;padding:0}
details{margin:14px 0}
summary{cursor:pointer;font-weight:600}
.head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:8px}
.avatar{width:96px;height:96px;border-radius:50%;object-fit:cover;margin:0;flex:none;border:2px solid var(--line);background:#fff}
.avatar.ph{display:flex;align-items:center;justify-content:center;background:#1F4E78;color:#fff;font-size:42px;font-weight:700}
.spec{border-left:4px solid var(--accent)}
.spec p{margin:6px 0 0}
.theme-toggle{display:inline-flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 10px;margin:0 0 12px}
.theme-toggle input{width:15px;height:15px;cursor:pointer}
"""

_REL_CLASS = {"высокая": "hi", "средняя": "mid", "низкая": "lo"}

class Raw(str):
    """Готовый HTML: не экранируется."""

# ---------- Мелкие помощники ----------

def esc(x) -> str:
    return html.escape(str(x), quote=True)

def _bad(x) -> bool:
    return x is None or (isinstance(x, float) and x != x)

def num(x, d: int = 0) -> str:
    if _bad(x):
        return "—"
    return f"{x:,.{d}f}".replace(",", " ")

def pct(x, sign: bool = False) -> str:
    if _bad(x):
        return "—"
    return f"{x * 100:+.1f}%" if sign else f"{x * 100:.1f}%"

def badge(level: str) -> str:
    return f'<span class="badge b-{_REL_CLASS.get(level, "lo")}">{esc(level)}</span>'

def ul(items) -> str:
    return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>"

def h2(text: str, rel=None) -> str:
    extra = f' <span class="muted">достоверность:</span> {badge(rel["level"])}' if rel else ""
    return f"<h2>{esc(text)}{extra}</h2>"

def card(label, value, hint: str = "") -> str:
    h = f'<div class="h">{esc(hint)}</div>' if hint else ""
    return f'<div class="card"><div class="l">{esc(label)}</div><div class="v">{esc(value)}</div>{h}</div>'

def img(png, alt: str) -> str:
    if png is None:
        return ""
    return f'<img class="chart" alt="{esc(alt)}" src="data:image/png;base64,{C.b64(png)}">'

def _cell(v) -> str:
    return str(v) if isinstance(v, Raw) else esc(v)

def table(headers, rows, num_cols=()) -> str:
    th = "".join(
        f'<th class="n">{esc(h)}</th>' if i in num_cols else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(
            f'<td class="n">{_cell(v)}</td>' if i in num_cols else f"<td>{_cell(v)}</td>"
            for i, v in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<div class="tblwrap"><table><tr>{th}</tr>{body}</table></div>'

_THEME_HEAD_JS = (
    "<script>(function(){var t='dark';"
    "try{t=localStorage.getItem('theme')||'dark';}catch(e){}"
    "document.documentElement.setAttribute('data-theme',t);})();</script>"
)

_THEME_TOGGLE = (
    '<label class="theme-toggle"><input type="checkbox" id="theme-dark"> Тёмная тема</label>'
    "<script>(function(){var cb=document.getElementById('theme-dark');"
    "cb.checked=document.documentElement.getAttribute('data-theme')!=='light';"
    "cb.addEventListener('change',function(){var t=cb.checked?'dark':'light';"
    "document.documentElement.setAttribute('data-theme',t);"
    "try{localStorage.setItem('theme',t);}catch(e){}});})();</script>"
)

def page(title: str, body: str) -> str:
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    return (
        '<!doctype html><html lang="ru" data-theme="dark"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{_THEME_HEAD_JS}"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">"
        f'{_THEME_TOGGLE}{body}<p class="muted">Сформировано: {stamp}</p></div></body></html>'
    )

def _link(uid, name, linked, prefix: str = "users/"):
    if uid in linked:
        return Raw(f'<a href="{prefix}{quote(linked[uid])}">{esc(name)}</a>')
    return name

def _week_range(monday, ds) -> str:
    """Подпись недели: границы подрезаны по окну анализа, например «27.09 – 27.09»."""
    start, end = monday, monday + timedelta(days=6)
    a, b = ds.window
    if a and start < a:
        start = a
    if b and end > b:
        end = b
    return f"{start:%d.%m} – {end:%d.%m}"

def _trend_lines(tr) -> list:
    pk, w = tr["peak"], tr["window"]
    lines = [
        f"Последние {w} дн.: {num(tr['last_avg'], 1)} в день; предыдущие {w} дн.: "
        f"{num(tr['prev_avg'], 1)} ({pct(tr['recent_change'], True)}).",
        f"Вторая половина окна к первой: {pct(tr['change'], True)} "
        f"(в среднем в день {num(tr['first_half_avg'], 1)} → {num(tr['second_half_avg'], 1)}).",
        f"Самая активная неделя: {pk['start']:%d.%m} – {pk['end']:%d.%m}, "
        f"{num(pk['avg'], 1)} в день, {pct(pk['share'])} всех сообщений окна.",
    ]
    if tr["direction"] == "всплеск и спад":
        fw = tr["first_week_avg"]
        lines.append(
            "Сравнение половин здесь вводит в заблуждение: его определяет эта неделя. "
            f"Последняя неделя в {pk['avg'] / max(tr['last_avg'], 1e-9):.1f} раза ниже пика"
            + (f" и составляет {pct(tr['last_avg'] / fw)} от уровня первой недели ({num(fw, 1)} в день)."
               if fw > 0 else ".")
            + " Устойчивого роста нет.")
    elif tr["direction"] == "неоднозначно":
        lines.append("Сигналы разных периодов противоположны: делать вывод о направлении рано.")
    return lines

def _trend_hint(r) -> str:
    t = r["trend"]
    if t == "пачками":
        return f"3 дня дали {pct(r['burst_share'])} сообщений"
    if t in ("растёт", "снижается", "стабильно"):
        return f"{int(r['recent_n'])} против {int(r['prev_n'])} за {int(r['trend_window'])} дн."
    if t == "притих":
        return f"нет сообщений {int(r['days_since_last'])} дн."
    return ""

def method_html() -> str:
    W = S.SZP_WEIGHTS

    def p(k):
        return f"{W[k] * 100:.0f}%"

    return f"""<details><summary>Как считаются СЗП и риск потери</summary><div class="box">
<p><b>СЗП</b> — степень значимости пользователя для сервера, число от 0 до 100: насколько изменится жизнь сервера, если этот человек пропадёт. Сумма пяти компонентов (каждый от 0 до 1) с весами.</p>
<ul>
<li><b>V, объём ({p('V')})</b>: √(доля человека в сообщениях сервера ÷ доля лидера). Лидер сервера получает 1.</li>
<li><b>C, постоянство ({p('C')})</b>: {S.SZP_C_ALL_W:.1f} × (активных дней ÷ дней от начала сервера) + {1 - S.SZP_C_ALL_W:.1f} × (активных дней из последних {S.SZP_RECENT_DAYS} ÷ {S.SZP_RECENT_DAYS}). Считается, только если с начала сервера прошло не меньше {S.SZP_MIN_DAYS} дн.</li>
<li><b>O, отклик ({p('O')})</b>: {S.SZP_O_APPEAL_W * 100:.0f}% — входящие обращения (ответы и теги) с учётом того, сколько разных людей к нему обращалось; {(1 - S.SZP_O_APPEAL_W) * 100:.0f}% — полученные реакции. Оба показателя берутся как √(значение ÷ максимум по серверу). Если сообщений меньше {S.SZP_O_MIN_MSGS}, отклик пропорционально снижается.</li>
<li><b>G, управление ({p('G')})</b>: {S.SZP_G_ROLE_W * 100:.0f}% — роль (основатель {S.SZP_ROLE['owner']}, админ {S.SZP_ROLE['admin']}, неформальная роль — вручную в настройках); {(1 - S.SZP_G_ROLE_W) * 100:.0f}% — реальная работа по журналу аудита: ln(1 + взвешенные действия) относительно лидера. Бан и кик весят больше, правки прав и приглашения меньше.</li>
<li><b>B, охват ({p('B')})</b>: доля значимых каналов (не менее {S.SZP_SIG_CHANNEL * 100:.0f}% сообщений сервера), где у человека не меньше {S.SZP_B_MIN_MSGS} сообщений.</li>
</ul>
<p>Динамика в СЗП отдельно не входит (кроме присутствия в последние дни внутри C). Если часть данных недоступна (старый формат файла, молодой сервер, нет журнала аудита), вес недостающих компонентов перераспределяется на остальные. В суммарном файле дни считаются от самой ранней даты начала серверов, роль берётся лучшая по серверам, действия аудита суммируются.</p>
<p><b>Риск потери</b> (справочная шкала 0–100): {S.RISK_WEIGHTS['recency'] * 100:.0f}% — давность молчания относительно обычного промежутка между активными днями человека,
{S.RISK_WEIGHTS['decline'] * 100:.0f}% — снижение темпа во второй половине окна, {S.RISK_WEIGHTS['streak'] * 100:.0f}% — прерванная серия активных дней. Показывается только для выбранных пользователей.</p>
<p class="muted">Веса и пороги заданы экспертно и сверены только с ручной оценкой на малых серверах. Реакции собраны в режиме «счётчики», в них входят реакции ботов и самореакции. Журнал аудита Discord хранит около {S.AUDIT_RETENTION_DAYS} дней. СЗП сравнивает людей внутри одного сервера за выбранный период; это не оценка человека и не основание для санкций. При короткой истории все выводы ориентировочные.</p>
</div></details>"""

# ---------- СЗП с переключателем «Учёт администрации» ----------

_SZP_COLORS = ["#2E7BB8", "#2E9E5B", "#9B59B6", "#E4A62E", "#8A8F98"]   # как в charts.chart_szp_top
_SZP_CHART_TOP = 15

_SZP_STYLE = """<style>
.szp-toggle{display:inline-flex;align-items:center;gap:8px;font-weight:600;cursor:pointer;
background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 12px;margin:6px 0}
.szp-toggle input{width:16px;height:16px;cursor:pointer}
.szp-legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted);margin:10px 0 6px}
.szp-dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.szp-row{display:grid;grid-template-columns:210px 1fr 48px;gap:8px;align-items:center;margin:3px 0;font-size:13px}
.szp-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.szp-track{display:flex;height:16px;background:var(--track);border-radius:4px;overflow:hidden}
.szp-seg{height:100%}
.szp-val{text-align:right;font-variant-numeric:tabular-nums}
</style>"""

def _szp_data(sk, um, linked) -> list:
    """Данные для переключателя: баллы каждого компонента и места «с администрацией» и «без»."""
    keys = list(S.SZP_WEIGHTS)                      # V, C, O, G, B
    w = np.array([S.SZP_WEIGHTS[k] for k in keys], dtype=float)
    comps = sk[keys].astype(float)
    den = (comps.notna().values * w).sum(axis=1)    # как в scoring.compute_szp: вес недостающих уходит остальным
    den = np.where(den > 0, den, 1.0)
    pts = np.nan_to_num(comps.values) * w / den[:, None] * 100
    g_pts = pts[:, keys.index("G")]

    szp_on = sk["szp"].astype(float)
    szp_off = (szp_on - g_pts).clip(lower=0)
    rank_off = szp_off.rank(method="min", ascending=False).astype(int)

    top_on = set(szp_on.sort_values(ascending=False, kind="stable").head(_SZP_CHART_TOP).index)
    top_off = set(szp_off.sort_values(ascending=False, kind="stable").head(_SZP_CHART_TOP).index)

    out = []
    for pos, uid in enumerate(sk.index):
        s = sk.loc[uid]
        sel = bool(s["selected"])
        if not (sel or uid in top_on or uid in top_off):
            continue
        r = um.loc[uid]
        risk = "—" if (not sel or _bad(s["risk"])) else f"{s['risk']:.0f} ({s['risk_level']})"
        out.append({
            "id": str(uid), "name": str(r["name"]),
            "href": ("users/" + quote(linked[uid])) if uid in linked else "",
            "sel": sel,
            "szp": round(float(szp_on[uid]), 2), "off": round(float(szp_off[uid]), 2),
            "pts": [round(float(x), 3) for x in pts[pos]],
            "rk": [int(s["szp_rank"]), int(rank_off[uid])],
            "msgs": int(r["messages"]), "days": int(r["active_days"]),
            "risk": risk, "rel": str(r["reliability"]), "role": str(r["role"] or ""),
            "low": r["reliability"] == "низкая",
        })
    return out

_SZP_JS = """<script>
(function(){
var DATA=__DATA__, CFG=__CFG__;
var cb=document.getElementById('szp-admin');
var bars=document.getElementById('szp-bars');
var legend=document.getElementById('szp-legend');
var tbody=document.getElementById('szp-body');
var note=document.getElementById('szp-note');

function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function lvl(v){for(var i=0;i<CFG.levels.length;i++){if(v>=CFG.levels[i][0])return CFG.levels[i][1];}return CFG.levels[CFG.levels.length-1][1];}

function render(){
  var on=cb.checked;
  var items=DATA.map(function(d){return {d:d,v:on?d.szp:d.off,rk:on?d.rk[0]:d.rk[1]};});
  items.sort(function(a,b){return (a.rk-b.rk)||(b.v-a.v);});

  note.textContent=on
    ? 'Учтены все пять компонентов: V, C, O, G, B.'
    : 'Баллы компонента G («Управление»: роль и работа по журналу аудита) сняты у всех; остальные компоненты не изменились. '
      +'Максимум шкалы уменьшился на '+CFG.wg+' баллов.';

  legend.textContent='';
  CFG.keys.forEach(function(k,i){
    if(!on&&i===CFG.gi)return;
    var item=el('span');
    var dot=el('span','szp-dot');dot.style.background=CFG.colors[i];
    item.appendChild(dot);item.appendChild(document.createTextNode(k+' · '+CFG.names[i]));
    legend.appendChild(item);
  });

  var top=items.slice(0,CFG.top), mx=10;
  top.forEach(function(x){if(x.v>mx)mx=x.v;});
  mx*=1.05;
  bars.textContent='';
  top.forEach(function(x){
    var row=el('div','szp-row');
    var nm=el('div','szp-name',x.d.name);nm.title=x.d.name;
    var track=el('div','szp-track');
    x.d.pts.forEach(function(p,i){
      if(!on&&i===CFG.gi)return;
      var seg=el('div','szp-seg');
      seg.style.width=(p/mx*100)+'%';
      seg.style.background=CFG.colors[i];
      seg.title=CFG.keys[i]+' · '+CFG.names[i]+': '+p.toFixed(1);
      track.appendChild(seg);
    });
    row.appendChild(nm);row.appendChild(track);row.appendChild(el('div','szp-val',x.v.toFixed(1)));
    bars.appendChild(row);
  });

  tbody.textContent='';
  items.forEach(function(x){
    if(!x.d.sel)return;
    var tr=el('tr');
    function td(text,num){var c=el('td',num?'n':'',text);tr.appendChild(c);return c;}
    td(String(x.rk),true);
    var c=el('td');
    if(x.d.href){var a=el('a','',x.d.name);a.href=x.d.href;c.appendChild(a);}else{c.textContent=x.d.name;}
    tr.appendChild(c);
    td(x.v.toFixed(1)+(x.d.low?' ≈':''),true);
    td(lvl(x.v));
    td(String(x.d.msgs),true);
    td(String(x.d.days),true);
    td(x.d.risk);
    td(x.d.rel);
    td(x.d.role);
    tbody.appendChild(tr);
  });
}
cb.addEventListener('change',render);
render();
})();
</script>"""

def szp_widget_html(sk, um, linked) -> str:
    """Галочка «Учёт администрации» + полоски топа + таблица. Пересчёт происходит в браузере."""
    keys = list(S.SZP_WEIGHTS)
    cfg = {
        "keys": keys, "names": [S.SZP_NAMES[k] for k in keys], "colors": _SZP_COLORS,
        "gi": keys.index("G"), "levels": [[t, n] for t, n in S.SZP_LEVELS],
        "top": _SZP_CHART_TOP, "wg": round(S.SZP_WEIGHTS["G"] * 100),
    }

    def dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=True).replace("</", "<\\/")

    head = "".join(
        f'<th class="n">{esc(h)}</th>' if i in (0, 2, 4, 5) else f"<th>{esc(h)}</th>"
        for i, h in enumerate(["№", "Пользователь", "СЗП", "Уровень", "Сообщений", "Дней",
                               "Риск потери", "Достоверность", "Роль"]))
    js = _SZP_JS.replace("__DATA__", dump(_szp_data(sk, um, linked))).replace("__CFG__", dump(cfg))
    return (
        _SZP_STYLE
        + '<label class="szp-toggle"><input type="checkbox" id="szp-admin" checked> Учёт администрации</label>'
        + '<p class="muted" id="szp-note"></p>'
        + '<p class="muted">В таблице — выбранные пользователи: топ по СЗП и все, у кого СЗП выше порога '
          "(для них есть индивидуальные отчёты). Знак «≈» — данных о человеке мало. "
          "Полный список — в сводном xlsx. Индивидуальные отчёты всегда показывают СЗП с учётом администрации.</p>"
        + '<div class="szp-legend" id="szp-legend"></div>'
        + '<div id="szp-bars"></div>'
        + f'<div class="tblwrap"><table><thead><tr>{head}</tr></thead><tbody id="szp-body"></tbody></table></div>'
        + js
    )

# ---------- Отчёт по серверу ----------

def build_server_html(ds, sm, um, sk, linked, emoji_html: str = "") -> str:
    rel = sm["reliability"]
    kind = "Суммарный отчёт" if ds.is_merged else "Отчёт по серверу"
    b = [
        f"<h1>{esc(ds.title)}</h1>",
        f'<p class="muted">{kind} · период: {esc(ds.period_text or "—")} · '
        f'окно анализа: {ds.window[0]} — {ds.window[1]} ({ds.window_days} дн.) · '
        f'часовой пояс: {esc(ds.tz or "—")}</p>',
    ]
    box = f'<b>Достоверность выводов:</b> {badge(rel["level"])}'
    if rel["notes"]:
        box += ul(rel["notes"])
    b.append(f'<div class="box">{box}</div>')
    if ds.warnings:
        b.append(f'<div class="box warn"><b>Предупреждения при чтении файла</b>{ul(ds.warnings)}</div>')
    if ds.notes:
        b.append(f'<div class="box"><b>Примечания бота</b>{ul(ds.notes)}</div>')
    if ds.excluded:
        b.append(f'<div class="box muted">Из анализа исключено чатов по правилам бота: {len(ds.excluded)}. '
                 "Их сообщения в отчёт не входят.</div>")

    if sm["empty"]:
        b.append("<p>За выбранный период сообщений нет.</p>")
        return page(f"{ds.title} — анализ", "".join(b))

    ov = sm["overview"]
    b.append('<div class="cards">' + "".join([
        card("Сообщений", num(sm["messages"])),
        card("Пользователей", num(sm["users"])),
        card("Дней в окне", sm["days"], f"с сообщениями: {ov['active_days']}"),
        card("В среднем в день", num(ov["avg_per_day"], 1), f"медиана {num(ov['median_per_day'])}"),
        card("DAU (среднее)", num(ov["avg_dau"], 1), f"максимум {ov['max_dau']}"),
        card("Доля ответов", pct(ov["reply_share"])),
        card("Пиковый день", str(ov["peak_day"]), f"{ov['peak_messages']} сообщ."),
    ]) + "</div>")

    # Динамика
    tr = sm["trend"]
    b.append(h2("Динамика активности", tr["rel"]))
    b.append(img(C.chart_timeline(sm["series"]), "Сообщения по дням"))
    b.append(img(C.chart_dau(sm["series"]), "DAU"))
    if tr["available"]:
        b.append(f"<p>Тренд: <b>{esc(tr['direction'])}</b>.</p>")
        b.append(ul(_trend_lines(tr)))
        b.append(img(C.chart_weekdays(tr["weekday_avg"]), "По дням недели"))
        rows = [[_week_range(wk, ds), int(r["messages"]), int(r["days"]),
                 num(r["messages"] / r["days"], 1)]
                for wk, r in tr["weekly"].iterrows()]
        b.append('<p class="muted">По неделям (границы подрезаны по окну анализа). Первая и последняя '
                 "недели могут быть неполными, поэтому сравнивайте их по колонке «В среднем в день».</p>")
        b.append(table(["Период", "Сообщений", "Дней", "В среднем в день"], rows,
                       num_cols=(1, 2, 3)))
    else:
        b.append(f"<p class=\"muted\">{esc(tr['rel']['notes'][0])}</p>")

    # Распределение
    d = sm["distribution"]
    b.append(h2("Распределение активности", d["rel"]))
    b.append(ul([
        f"Индекс Джини: {d['gini']:.2f} (0 — все пишут поровну, 1 — пишет один человек).",
        f"Доля топ-1: {pct(d['top1'])}, топ-5: {pct(d['top5'])}, топ-10% ({d['k10']} чел.): {pct(d['top10pct'])}.",
        f"50% сообщений пишут {d['users_for_50']} чел., 80% — {d['users_for_80']} чел.",
        f"Сообщений на человека: среднее {num(d['mean'], 1)}, медиана {num(d['median'])}, "
        f"90-й перцентиль {num(d['p90'])}, максимум {num(d['max'])}.",
    ]))
    b.append(img(C.chart_lorenz(um["messages"].values, d["gini"]), "Кривая Лоренца"))

    seg = sm["segments"]
    b.append("<h3>Сегменты пользователей</h3>")
    b.append('<p class="muted">Ядро — активны ≥50% дней окна; регулярные — 20–50%; '
             "эпизодические — менее 20%; разовые — писали в один день.</p>")
    b.append(table(["Сегмент", "Человек", "Доля людей", "Сообщений", "Доля сообщений"],
                   [[name, int(r["users"]), pct(r["user_share"]), int(r["messages"]), pct(r["message_share"])]
                    for name, r in seg.iterrows()], num_cols=(1, 2, 3, 4)))
    b.append(img(C.chart_segments(seg), "Сегменты"))

    # Время
    t = sm["time"]
    if t:
        b.append(h2("Время активности", t["rel"]))
        b.append(ul([
            f"Пик по часам: {t['peak_hour']:02d}:00; самый активный день недели: {S.WEEKDAYS[t['peak_weekday']]}.",
            f"Самые активные 3 часа подряд: с {t['best3_start']:02d}:00 ({pct(t['best3_share'])} сообщений).",
            f"Ночью (00–06): {pct(t['night_share'])}; в выходные: {pct(t['weekend_share'])}.",
        ]))
        b.append(img(C.chart_heatmap(t["matrix"], ds.tz), "Тепловая карта"))

    # Каналы
    chn = sm["channels"]
    if chn:
        b.append(h2("Каналы", chn["rel"]))
        b.append(ul([
            f"Топ-3 канала дают {pct(chn['top3_share'])} сообщений.",
            f"Пишут только в одном канале: {pct(chn['single_channel_share'])} пользователей; "
            f"в среднем каналов на человека: {chn['mean_channels']:.1f}.",
        ]))
        b.append(img(C.chart_channels(chn["table"]), "Топ каналов"))
        b.append(table(["Канал", "Сообщений", "Доля", "Авторов"],
                       [[r["channel"], int(r["messages"]), pct(r["share"]), int(r["authors"])]
                        for _cid, r in chn["table"].head(15).iterrows()], num_cols=(1, 2, 3)))

    if emoji_html:
        b.append(emoji_html)

    # Жизненный цикл и сравнение
    lc = sm["lifecycle"]
    b.append(h2("Жизненный цикл", lc["rel"]))
    items = [f"Притихли (нет сообщений последние {M.DORMANT_DAYS} дн. окна): {lc['dormant']} чел. ({pct(lc['dormant_share'])})."]
    items.append(f"Писали уже в первый день окна ({ds.window[0]}): {lc['from_start']} чел. "
                 "Это стартовая аудитория, а не приток новых.")
    if "retained" in lc:
        items.append(f"Писали в обеих половинах окна: {lc['retained']} чел.; только в первой: {lc['lost']}; "
                     f"только во второй: {lc['new_second_half']}; удержание: {pct(lc.get('retention'))}.")
    b.append(ul(items))
    b.append(ul(lc["rel"]["notes"]))
    if lc["new_by_week"]:
        b.append("<p class=\"muted\">Впервые написали после первого дня окна, по неделям:</p>")
        b.append(table(["Период", "Впервые написали"],
                       [[_week_range(wk, ds), v] for wk, v in sorted(lc["new_by_week"].items())],
                       num_cols=(1,)))

    cp = sm["compare"]
    if cp:
        b.append(h2("Сравнение с предыдущим периодом", cp["rel"]))
        b.append(ul([
            f"Сообщения: {num(cp['prev_messages'])} → {num(sm['messages'])} ({pct(cp['messages_change'], True)}).",
            f"Пользователи: {num(cp['prev_users'])} → {num(sm['users'])} ({pct(cp['users_change'], True)}).",
            f"Писали в обоих периодах: {cp['retained']}; новых: {cp['new']}; не писавших сейчас: {cp['lost']}; "
            f"удержание: {pct(cp['retention'])}.",
        ]))
        b.append(ul(cp["rel"]["notes"]))

    # Серверы (суммарный файл)
    sv = sm["servers"]
    if sv:
        b.append(h2("Серверы"))
        b.append(f"<p>Пишут на нескольких серверах: {sv['multi_server_users']} чел. "
                 f"({pct(sv['multi_server_share'])}).</p>")
        b.append(table(["Сервер", "Сообщений", "Доля", "Авторов"],
                       [[srv, int(r["messages"]), pct(r["share"]), int(r["authors"])]
                        for srv, r in sv["table"].iterrows()], num_cols=(1, 2, 3)))

    # Администрация
    if len(ds.staff):
        st = sm["staff"]
        b.append(h2("Администрация"))
        if st:
            b.append(f"<p>{st['people']} чел. написали {num(st['messages'])} сообщений — "
                     f"{pct(st['share'])} от всех.</p>")
        s2 = ds.staff.copy()
        s2["k"] = s2["kind"].map({"owner": 0, "admin": 1})
        s2 = s2.sort_values(["server", "k", "n"], ascending=[True, True, False])
        b.append(table(["Сервер", "Пользователь", "Должность", "Роли", "Сообщений"],
                       [[r.server, r.name, S.KIND_RU.get(r.kind, r.kind), r.roles or "—", int(r.n)]
                        for r in s2.itertuples()], num_cols=(4,)))

    if len(ds.admin_events):
        if not len(ds.staff):
            b.append(h2("Администрация"))
        b.append("<h3>Ключевые события администрации</h3>")
        b.append('<p class="muted">Участник — кому выдали / у кого сняли роль (при передаче — прежний основатель); '
                 "Участник 2 — кто это сделал (при передаче — новый основатель).</p>")
        head = ["Событие", "Дата", "Участник", "Участник 2"] + (["Сервер"] if ds.is_merged else [])
        rows = []
        for e in ds.admin_events.itertuples():
            text = S.ADMIN_EVENT_LABELS.get(e.kind, e.kind) + (f" «{e.role}»" if e.role else "")
            row = [text, f"{e.date:%d.%m.%Y}", e.who, e.who2]
            if ds.is_merged:
                row.append(e.server)
            rows.append(row)
        b.append(table(head, rows))

    # СЗП
    if len(sk):
        b.append(h2("СЗП: значимость пользователей", rel))
        b.append(szp_widget_html(sk, um, linked))
        b.append(method_html())

    return page(f"{ds.title} — анализ", "".join(b))

# ---------- Индивидуальный отчёт ----------

def _explain(ds, s, r, D) -> dict:
    """Короткие пояснения к компонентам СЗП для индивидуального отчёта."""
    a = int(r["active_days"])
    msgs = int(r["messages"])

    if _bad(s["C"]):
        c = f"не считается: с начала сервера прошло меньше {S.SZP_MIN_DAYS} дн."
    else:
        c = (f"активных дней {a} из {int(s['d_total'])} ({pct(a / s['d_total'])}); "
             f"из последних {S.SZP_RECENT_DAYS} дн. активен {int(s['d_recent'])}")

    if _bad(s["O"]):
        o = "нет данных об обращениях и реакциях (файл старого формата)"
    else:
        o = (f"обращений к нему {int(s['appeals_in'])} от {int(s['addressers'])} чел.; "
             f"реакций получено {int(s['rx_recv_n'])}")
        if msgs < S.SZP_O_MIN_MSGS:
            o += f"; сообщений меньше {S.SZP_O_MIN_MSGS}, поэтому отклик снижен"

    aud = ("журнал аудита не собран" if _bad(s["g_audit"])
           else f"действий по журналу аудита (взвешенно): {s['g_audit']:.1f}")
    g = f"роль: {r['role'] or 'без роли администрации'} (значение {s['g_role']:.2f}); {aud}"

    if _bad(s["B"]):
        b = "нет данных по каналам (файл старого формата)"
    else:
        b = (f"значимых каналов, где ≥{S.SZP_B_MIN_MSGS} сообщений: "
             f"{int(s['b_present'])} из {int(s['b_sig'])}")

    return {
        "V": f"{msgs} сообщений = {pct(s['v_share'])} сообщений сервера; оценка относительно лидера сервера",
        "C": c, "O": o, "G": g, "B": b,
    }

def _reply_section(ds, uid, r) -> str:
    """Раздел «Общение»: к кому обращается и кто обращается к нему (ответы + теги)."""
    ap = ds.address_pairs
    if ap is None or ap.empty:
        return ""
    out_df = ap[ap["src"] == uid]
    in_df = ap[ap["dst"] == uid]
    if out_df.empty and in_df.empty:
        return ""

    names = ds.users.drop_duplicates("uid").set_index("uid")["name"].to_dict()

    def nm(u):
        return names.get(u) or u

    def pairs(df):
        return dict(zip(zip(df["src"], df["dst"]), df["n"]))

    rep = pairs(ds.reply_pairs)      # (src, dst) -> ответов
    men = pairs(ds.mention_pairs)    # (src, dst) -> тегов

    out_total, in_total = int(out_df["n"].sum()), int(in_df["n"].sum())
    my_to = dict(zip(out_df["dst"], out_df["n"]))      # сколько обращений он направил каждому
    to_me = dict(zip(in_df["src"], in_df["n"]))        # сколько обращений каждый направил ему
    msgs = max(int(r["messages"]), 1)

    parts = [h2("Общение (ответы и теги)")]
    parts.append('<div class="cards">' + "".join([
        card("Получено обращений", num(in_total), f"от {in_df['src'].nunique()} чел."),
        card("Обратился(лась) сам(а)", num(out_total), f"к {out_df['dst'].nunique()} чел."),
        card("Обращений на 1 сообщение", num(in_total / msgs, 2)),
        card("Взаимность", pct(r["reciprocity"]), "обращения к тем, кто обращался к нему"),
    ]) + "</div>")

    if not out_df.empty:
        parts.append("<h3>К кому обращается чаще всего</h3>")
        top = out_df.sort_values("n", ascending=False).head(8)
        parts.append(table(
            ["Пользователь", "Обращений", "Доля его обращений", "из них ответов", "из них тегов",
             "Обратился(лась) к нему в ответ"],
            [[nm(x.dst), int(x.n), pct(x.n / out_total),
              int(rep.get((uid, x.dst), 0)), int(men.get((uid, x.dst), 0)),
              int(to_me.get(x.dst, 0))]
             for x in top.itertuples()],
            num_cols=(1, 2, 3, 4, 5)))

    if not in_df.empty:
        parts.append("<h3>Кто обращается к нему чаще всего</h3>")
        top = in_df.sort_values("n", ascending=False).head(8)
        parts.append(table(
            ["Пользователь", "Обращений к нему", "Доля всех обращений к нему", "из них ответов",
             "из них тегов", "Он(а) обратился(лась) в ответ"],
            [[nm(x.src), int(x.n), pct(x.n / in_total),
              int(rep.get((x.src, uid), 0)), int(men.get((x.src, uid), 0)),
              int(my_to.get(x.src, 0))]
             for x in top.itertuples()],
            num_cols=(1, 2, 3, 4, 5)))

    known = int(ds.reply_pairs["n"].sum()) if len(ds.reply_pairs) else 0
    lost = ds.reply_unresolved + ds.reply_self_or_bot
    total_all = known + lost
    note = ("Обращение — это ответ (кнопка «Ответить») или тег конкретного человека. "
            "Теги ролей, @everyone, @here, ботов и самого себя не учитываются. "
            "Ответ, в котором тегнут его же адресат, считается одним обращением. ")
    if total_all:
        note += (f"Ответов, где известен адресат: {num(known)} из {num(total_all)} ({pct(known / total_all)}) "
                 "по серверу; ответы на удалённые сообщения, самому себе и ботам не учтены. ")
    note += "Общение без ответа и без тега здесь не видно, поэтому цифры — нижняя оценка."
    parts.append(f'<p class="muted">{note}</p>')
    return "".join(parts)

def build_user_html(ds, uid, um, sk, days, avatar=None, emoji_html: str = "") -> str:
    r, s = um.loc[uid], sk.loc[uid]
    D = len(days)
    W = S.SZP_WEIGHTS
    b = ['<p class="muted"><a href="../report.html">← к отчёту по серверу</a></p>']
    sub = [x for x in (f"@{r['username']}" if r["username"] else "", f"ID {uid}", r["role"],
                       ds.title, ds.period_text) if x]

    if avatar:
        av = f'<img class="avatar" alt="" src="{avatar}">'
    else:
        letter = (str(r["name"]).strip() or "?")[0].upper()
        av = f'<div class="avatar ph">{esc(letter)}</div>'
    b.append(f'<div class="head"><div><h1>{esc(r["name"])}</h1>'
             f'<p class="muted">{esc(" · ".join(sub))}</p></div>{av}</div>')

    # Особая информация (заполняется вручную в settings.SPEC_INFO)
    spec = S.SPEC_INFO_BY_ID.get(str(uid))
    if spec:
        spec_html = esc(spec).replace(chr(10), "<br>")
        b.append(f'<div class="box spec"><b>Особая информация</b><p>{spec_html}</p></div>')

    lvl = r["reliability"]
    box = f"<b>Достоверность выводов:</b> {badge(lvl)}"
    if lvl == "низкая":
        box += "<br>Данных об этом человеке мало (сообщений или активных дней): оценки ниже ориентировочные."
    if r["burst_share"] >= M.BURST_TOP3_SHARE and r["active_days"] >= M.MIN_USER_ACTIVE_DAYS_TREND:
        box += (f"<br>Активность пачками: три самых активных дня дали {pct(r['burst_share'])} "
                "сообщений, поэтому выводы о динамике и привычном времени ориентировочные.")
    if ds.incomplete:
        box += "<br>Сбор данных был остановлен вручную: все цифры неполные."
    if ds.filtered:
        box += f"<br>Отчёт построен с фильтром ({esc(ds.filtered)}): это не весь сервер."
    b.append(f'<div class="box">{box}</div>')

    cards = [
        card("СЗП", num(s["szp"], 1), s["szp_level"]),
        card("Место по сообщениям", f"{int(r['rank'])} из {len(um)}"),
        card("Сообщений", num(r["messages"]), f"{pct(r['server_share'])} от всех"),
        card("Активных дней", f"{int(r['active_days'])} из {D}", pct(r["active_share"])),
        card("В активный день", num(r["avg_per_active_day"], 1), f"максимум {int(r['max_day'])}"),
        card("Серия подряд", f"{int(r['longest_streak'])} дн.", f"сейчас {int(r['current_streak'])}"),
        card("Недель активно", int(r["weeks_active"]), pct(r["weeks_share"])),
        card("Последнее сообщение", str(r["last_date"]), f"{int(r['days_since_last'])} дн. до конца окна"),
        card("Тренд", r["trend"], _trend_hint(r)),
    ]
    if ds.full_data:
        cards.append(card("Ответы", pct(r["reply_share"]), f"{int(r['replies'])} шт."))
    b.append('<div class="cards">' + "".join(cards) + "</div>")

    # Компоненты СЗП
    b.append(h2("Из чего сложился СЗП"))
    expl = _explain(ds, s, r, D)
    rows = []
    for k in W:
        v = s[k]
        rows.append([k, S.SZP_NAMES[k], "н/д" if _bad(v) else f"{v:.2f}", f"{W[k] * 100:.0f}%",
                     "—" if _bad(v) else f"{W[k] * v * 100:.1f}", expl[k]])
    rows.append(["", "Итого", "", "100%", f"{s['szp']:.1f}", ""])
    b.append(table(["", "Компонент", "Значение (0–1)", "Вес", "Баллы", "Пояснение"], rows, num_cols=(2, 3, 4)))
    if s["szp_partial"]:
        b.append('<p class="muted">Часть компонентов недоступна, поэтому их вес перераспределён: '
                 "сумма баллов в таблице может не совпадать с итоговым СЗП.</p>")

    # Риск потери
    if s["selected"]:
        b.append(h2("Риск потери (справочно)"))
        b.append(f"<p>Риск: <b>{s['risk']:.0f}</b> из 100 ({esc(s['risk_level'])}). "
                 "Это не прогноз ухода человека, а сигнал присмотреться: он учитывает только "
                 "молчание, падение темпа и прерванную серию.</p>")
        b.append(table(["Составляющая", "Значение (0–1)", "Вес"],
                       [["Давность молчания", "н/д" if _bad(s["risk_recency"]) else f"{s['risk_recency']:.2f}",
                         f"{S.RISK_WEIGHTS['recency'] * 100:.0f}%"],
                        ["Снижение темпа", "н/д" if _bad(s["risk_decline"]) else f"{s['risk_decline']:.2f}",
                         f"{S.RISK_WEIGHTS['decline'] * 100:.0f}%"],
                        ["Прерванная серия", "н/д" if _bad(s["risk_streak"]) else f"{s['risk_streak']:.2f}",
                         f"{S.RISK_WEIGHTS['streak'] * 100:.0f}%"]], num_cols=(1, 2)))

    # Графики
    b.append(h2("Активность"))
    mine = ds.daily[ds.daily["uid"] == uid].set_index("date")["n"].reindex(days, fill_value=0)
    b.append(img(C.chart_user_days(mine, r["name"]), "Сообщения по дням"))
    b.append(img(C.chart_user_hours(ds, uid), "Профиль по часам"))
    if not _bad(r["peak_hour"]):
        wd = ("" if _bad(r["peak_weekday"])
              else f", чаще всего в день недели {S.WEEKDAYS[int(r['peak_weekday'])]}")
        b.append(f"<p>Чаще всего пишет около {int(r['peak_hour']):02d}:00{wd}; "
                 f"ночью — {pct(r['night_share'])}, в выходные — {pct(r['weekend_share'])} "
                 f"(часовой пояс {esc(ds.tz or '—')}).</p>")

    # Каналы
    chn = ds.channels
    if len(chn):
        mine_ch = chn[chn["uid"] == uid].sort_values("n", ascending=False)
        total = mine_ch["n"].sum()
        if total:
            b.append(h2("Где пишет"))
            b.append(table(["Канал", "Сообщений", "Доля"],
                           [[x.channel, int(x.n), pct(x.n / total)] for x in mine_ch.head(8).itertuples()],
                           num_cols=(1, 2)))
            if len(mine_ch) > 8:
                b.append(f'<p class="muted">Показаны 8 из {len(mine_ch)} каналов.</p>')

    b.append(_reply_section(ds, uid, r))
    if emoji_html:
        b.append(emoji_html)
    return page(f"{r['name']} — индивидуальный отчёт", "".join(b))