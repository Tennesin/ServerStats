"""Этап 4: графики (matplotlib -> PNG в байтах). Без Discord и без сети."""

import base64
import io
import re
import warnings

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

import settings as S

warnings.filterwarnings("ignore", message="Glyph .* missing")   # редкие символы в никах

BLUE, ORANGE, GREEN, GRAY = "#2E7BB8", "#E4572E", "#2E9E5B", "#8A8F98"
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.alpha": 0.25, "axes.axisbelow": True,
    "axes.titlesize": 12, "axes.titleweight": "bold",
})

_NON_BMP = re.compile("[\U00010000-\U0010FFFF]")

def _lab(text, width: int = 28) -> str:
    t = _NON_BMP.sub("", str(text)).strip() or "?"
    if len(t) > width:
        t = t[: width - 1] + "…"
    return t.replace("$", r"\$")

def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()

def b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")

def _fmt_dates(ax):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))

# ---------- Сервер ----------

def chart_timeline(series):
    if series is None or series.empty:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.bar(series.index, series["messages"], color=BLUE, alpha=0.55, width=0.8,
           label="Сообщений за день")
    if len(series) >= 7:
        ax.plot(series.index, series["messages"].rolling(7, min_periods=1).mean(),
                color=ORANGE, lw=2, label="Среднее за 7 дней")
    ax.set_title("Сообщения по дням")
    ax.legend(frameon=False)
    _fmt_dates(ax)
    return _png(fig)

def chart_dau(series):
    if series is None or series.empty:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.0))
    ax.fill_between(series.index, series["dau"], color=GREEN, alpha=0.2)
    ax.plot(series.index, series["dau"], color=GREEN, lw=2)
    ax.set_title("Активные пользователи по дням (DAU)")
    ax.set_ylim(bottom=0)
    _fmt_dates(ax)
    return _png(fig)

def chart_audit_days(df, days):
    if df is None or df.empty:
        return None
    s = df.set_index("date")["count"].reindex(days, fill_value=0)
    if s.sum() == 0:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.0))
    ax.bar(s.index, s.values, color=ORANGE, width=0.8)
    ax.set_title("Записи журнала аудита по дням")
    _fmt_dates(ax)
    return _png(fig)

def chart_weekdays(avg):
    if not avg or sum(avg) == 0:
        return None
    fig, ax = plt.subplots(figsize=(7, 3.0))
    ax.bar(S.WEEKDAYS, avg, color=BLUE)
    ax.set_title("Среднее число сообщений по дням недели")
    return _png(fig)

def chart_heatmap(matrix, tz: str = ""):
    if matrix is None or np.sum(matrix) == 0:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.4))
    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd")
    ax.set_yticks(range(7))
    ax.set_yticklabels(S.WEEKDAYS)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)])
    ax.grid(False)
    ax.set_xlabel("час" + (f" ({tz})" if tz else ""))
    ax.set_title("Активность: день недели × час")
    fig.colorbar(im, ax=ax, label="сообщений")
    return _png(fig)

def chart_lorenz(values, gini_value=None):
    x = np.sort(np.asarray(values, dtype=float))
    if len(x) < 2 or x.sum() == 0:
        return None
    cum = np.insert(np.cumsum(x), 0, 0) / x.sum()
    share = np.linspace(0, 1, len(cum))
    fig, ax = plt.subplots(figsize=(4.8, 4.4))
    ax.plot([0, 1], [0, 1], color=GRAY, ls="--", label="Равное распределение")
    ax.plot(share, cum, color=ORANGE, lw=2, label="Фактическое")
    ax.fill_between(share, cum, share, color=ORANGE, alpha=0.15)
    ax.set_xlabel("Доля пользователей (от наименее активных)")
    ax.set_ylabel("Доля сообщений")
    title = "Кривая Лоренца"
    if gini_value is not None:
        title += f" (Джини {gini_value:.2f})"
    ax.set_title(title)
    ax.legend(frameon=False, loc="upper left")
    return _png(fig)

def chart_segments(seg):
    if seg is None or seg.empty:
        return None
    x = np.arange(len(seg))
    w = 0.38
    fig, ax = plt.subplots(figsize=(7.5, 3.4))
    ax.bar(x - w / 2, seg["user_share"] * 100, w, color=BLUE, label="Доля пользователей")
    ax.bar(x + w / 2, seg["message_share"] * 100, w, color=ORANGE, label="Доля сообщений")
    ax.set_xticks(x)
    ax.set_xticklabels(list(seg.index))
    ax.set_ylabel("%")
    ax.set_title("Сегменты пользователей")
    ax.legend(frameon=False)
    return _png(fig)

def chart_channels(table, top: int = 10):
    if table is None or table.empty:
        return None
    t = table.head(top)
    fig, ax = plt.subplots(figsize=(9, 0.4 * len(t) + 1.2))
    ax.barh(range(len(t)), t["messages"], color=BLUE)
    ax.set_yticks(range(len(t)))
    ax.set_yticklabels([_lab(c, 40) for c in t["channel"]])
    ax.invert_yaxis()
    mx = float(t["messages"].max())
    for i, v in enumerate(t["messages"]):
        ax.text(v + mx * 0.01, i, f"{int(v):,}".replace(",", " "), va="center", fontsize=8)
    ax.set_xlim(0, mx * 1.12)
    ax.set_title(f"Топ каналов по сообщениям (до {top})")
    return _png(fig)

def chart_szp_top(sz, um, n: int = 15):
    if sz is None or sz.empty:
        return None
    top = sz.head(n)
    W = S.SZP_WEIGHTS
    colors = ["#2E7BB8", "#2E9E5B", "#9B59B6", "#E4A62E", "#8A8F98"]
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(top) + 1.8))
    left = np.zeros(len(top))
    for col, k in zip(colors, W):
        vals = top[k].fillna(0).values * W[k] * 100
        ax.barh(range(len(top)), vals, left=left, color=col, label=f"{k} · {S.SZP_NAMES[k]}")
        left += vals
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels([_lab(um.loc[u, "name"]) for u in top.index])
    ax.invert_yaxis()
    for i, v in enumerate(left):
        ax.text(v + 0.8, i, f"{v:.1f}", va="center", fontsize=8)
    ax.set_xlim(0, max(10.0, float(left.max()) * 1.12))
    ax.set_xlabel("баллы СЗП (вклад компонентов)")
    ax.set_title(f"Топ-{len(top)} по СЗП")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=3, frameon=False, fontsize=8)
    return _png(fig)

# ---------- Пользователь ----------

def chart_user_days(series, name: str = ""):
    """series: число сообщений по каждому дню окна (индекс — даты)."""
    if series is None or series.empty or series.sum() == 0:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.0))
    ax.bar(series.index, series.values, color=BLUE, width=0.8)
    ax.set_title("Сообщения по дням" + (f": {_lab(name, 40)}" if name else ""))
    _fmt_dates(ax)
    return _png(fig)

def chart_user_hours(ds, uid: str):
    ht = ds.heat
    if ht is None or ht.empty:
        return None
    mine = ht[ht["uid"] == uid].groupby("hour")["n"].sum().reindex(range(24), fill_value=0)
    if mine.sum() == 0:
        return None
    fig, ax = plt.subplots(figsize=(9, 2.8))
    ax.bar(range(24), mine.values, color=ORANGE)
    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{h:02d}" for h in range(24)], fontsize=8)
    ax.set_xlabel("час" + (f" ({ds.tz})" if ds.tz else ""))
    ax.set_title("Профиль активности по часам")
    return _png(fig)