"""Этап 3: СЗП (степень значимости пользователя для сервера) и справочная шкала «риск потери»."""

import numpy as np
import pandas as pd

import settings as S
from loader import Dataset
from metrics import calendar_days

def _label(value: float, levels) -> str:
    for threshold, name in levels:
        if value >= threshold:
            return name
    return levels[-1][1]

def audit_weight(action: str) -> float:
    """Вес одного действия из журнала аудита."""
    if str(action).startswith(S.SZP_AUDIT_IGNORE_PREFIX):
        return 0.0
    return float(S.SZP_AUDIT_WEIGHTS.get(action, S.SZP_AUDIT_DEFAULT))


def _sqrt_norm(s: pd.Series) -> pd.Series:
    """√(значение / максимум). Лидер получает 1; если максимум 0 — всем 0."""
    mx = float(s.max()) if len(s) else 0.0
    if mx <= 0:
        return pd.Series(0.0, index=s.index)
    return np.sqrt(s / mx)


def role_values(ds: Dataset, idx) -> pd.Series:
    """Роль 0..1: формальная (основатель/админ) или неформальная из настроек — что выше.
    В суммарном файле берётся максимум по всем серверам."""
    formal = pd.Series(0.0, index=idx)
    if len(ds.staff):
        w = ds.staff["kind"].map(S.SZP_ROLE)
        formal = w.groupby(ds.staff["uid"]).max().reindex(idx).fillna(0.0)

    informal = pd.Series(0.0, index=idx)
    tables = list(S.SZP_INFORMAL_ROLE.values()) if ds.is_merged \
        else [S.SZP_INFORMAL_ROLE.get(ds.guild_id, {})]
    for table in tables:
        for uid, val in table.items():
            u = str(uid)
            if u in informal.index:
                informal.at[u] = max(float(informal.at[u]), float(val))
    return pd.Series(np.maximum(formal.values, informal.values), index=idx)

def audit_work(ds: Dataset, idx):
    """Взвешенное число действий в журнале аудита по людям. None — журнал не собран."""
    if not ds.has_audit:
        return None
    a = ds.audit_users
    if a.empty:
        return pd.Series(0.0, index=idx)
    weighted = a["count"] * a["action"].map(audit_weight)
    return weighted.groupby(a["uid"]).sum().reindex(idx).fillna(0.0)

def compute_szp(ds: Dataset, um: pd.DataFrame,
                top_n: int = S.SELECT_TOP_N, min_szp: float = S.SELECT_MIN_SZP) -> pd.DataFrame:
    """Этап 3: СЗП (степень значимости пользователя для сервера)."""
    if um.empty:
        return pd.DataFrame()
    idx = um.index
    n = um["messages"].astype(float)
    nan = pd.Series(np.nan, index=idx)
    zero = pd.Series(0.0, index=idx)
    days = calendar_days(ds)
    Y = len(days)

    # V — объём: корень из доли в сообщениях сервера, лидер = 1
    share = n / n.sum()
    V = np.sqrt(share / share.max())

    # C — постоянство: активные дни из Y (от начала сервера) + присутствие в последние дни.
    # Если сервер/окно моложе SZP_MIN_DAYS, компонент не считается, его вес уходит остальным.
    C = nan.copy()
    recent = zero.copy()
    r_days = min(S.SZP_RECENT_DAYS, Y) if Y else 0
    if Y >= S.SZP_MIN_DAYS:
        since = days[-r_days]
        recent = (ds.daily[ds.daily["date"] >= since].groupby("uid")["date"].nunique()
                  .reindex(idx).fillna(0.0))
        share_days = (um["active_days"] / Y).clip(upper=1.0)
        C = S.SZP_C_ALL_W * share_days + (1 - S.SZP_C_ALL_W) * recent / r_days

    # O — отклик: входящие обращения (ответы + теги) с учётом широты и полученные реакции
    appeals_in, addressers, rx_recv = zero.copy(), zero.copy(), zero.copy()
    A, Rx = nan.copy(), nan.copy()
    pairs = ds.address_pairs
    if pairs is not None and not pairs.empty:
        p = pairs[pairs["src"].isin(idx) & pairs["dst"].isin(idx) & (pairs["src"] != pairs["dst"])]
        appeals_in = p.groupby("dst")["n"].sum().reindex(idx).fillna(0.0)
        addressers = p.groupby("dst")["src"].nunique().reindex(idx).fillna(0.0)
        breadth = (addressers / max(1, len(idx) - 1)).clip(upper=1.0)
        k = S.SZP_O_BREADTH_W
        A = _sqrt_norm(appeals_in) * ((1 - k) + k * breadth)
    if ds.has_reactions and len(ds.reaction_recv):
        rx_recv = ds.reaction_recv.groupby("uid")["n"].sum().reindex(idx).fillna(0.0)
        Rx = _sqrt_norm(rx_recv)
    parts = pd.DataFrame({"A": A, "R": Rx})
    pw = pd.Series({"A": S.SZP_O_APPEAL_W, "R": 1 - S.SZP_O_APPEAL_W})
    denom = (parts.notna() * pw).sum(axis=1)
    O = (parts.fillna(0.0) * pw).sum(axis=1) / denom.where(denom > 0)
    O = O * (n / S.SZP_O_MIN_MSGS).clip(upper=1.0)

    # G — управление: роль + реальная работа по журналу аудита
    role = role_values(ds, idx)
    aud_w = audit_work(ds, idx)
    audit_missing = aud_w is None
    if audit_missing:
        G = role
    else:
        mx = float(aud_w.max())
        aud_score = np.log1p(aud_w) / np.log1p(mx) if mx > 0 else zero
        G = S.SZP_G_ROLE_W * role + (1 - S.SZP_G_ROLE_W) * aud_score

    # B — охват: доля значимых каналов, где человек пишет
    B = nan.copy()
    present, n_sig = zero.copy(), 0
    ch = ds.channels[ds.channels["n"] > 0] if len(ds.channels) else ds.channels
    if len(ch):
        chan_tot = ch.groupby("channel_id")["n"].sum()
        sig = chan_tot[chan_tot / chan_tot.sum() >= S.SZP_SIG_CHANNEL].index
        n_sig = len(sig)
        if n_sig:
            cnt = ch[ch["channel_id"].isin(sig) & (ch["n"] >= S.SZP_B_MIN_MSGS)].groupby("uid").size()
            present = cnt.reindex(idx).fillna(0.0)
            B = present / n_sig

    comps = pd.DataFrame({"V": V, "C": C, "O": O, "G": G, "B": B}, index=idx)
    w = pd.Series(S.SZP_WEIGHTS)[list(comps.columns)]
    avail = comps.notna()
    # если компонента нет (старый формат файла, короткое окно), его вес перераспределяется на остальные
    szp = (comps.fillna(0.0) * w).sum(axis=1) / (avail * w).sum(axis=1) * 100

    out = comps.copy()
    out["szp"] = szp
    out["szp_partial"] = (~avail.all(axis=1)) | audit_missing
    out["szp_level"] = szp.map(lambda v: _label(v, S.SZP_LEVELS))
    out["szp_rank"] = szp.rank(method="min", ascending=False).astype(int)
    # Подробности для индивидуальных отчётов (имена не пересекаются с колонками user_metrics)
    out["v_share"] = share
    out["d_total"] = Y
    out["d_recent"] = recent
    out["appeals_in"] = appeals_in
    out["addressers"] = addressers
    out["rx_recv_n"] = rx_recv
    out["g_role"] = role
    out["g_audit"] = nan if audit_missing else aud_w
    out["b_present"] = present
    out["b_sig"] = n_sig

    out = out.sort_values("szp", ascending=False, kind="stable")
    order = out["szp"].rank(method="first", ascending=False)
    out["selected"] = (order <= top_n) | (out["szp"] >= min_szp)
    return out