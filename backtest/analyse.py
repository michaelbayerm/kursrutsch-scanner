"""
Auswertung des Kursrutsch-Backtests auf Basis von backtest/daten/signale.csv.gz.

Trade-Logik (alle Kurse dividendenbereinigt, Tag 0 = Signaltag, Signal nach Börsenschluss):
  Einstieg   je nach Regel zur Eröffnung eines späteren Tages
  Take-Profit Limit-Verkauf bei Einstieg × (1 + tp); öffnet der Kurs darüber, Verkauf zur Eröffnung
  Stop-Loss  Stop bei Einstieg × (1 − sl); öffnet der Kurs darunter, Verkauf zur Eröffnung (schlechter)
  Beides am selben Tag erreicht -> Stop zählt (vorsichtige Annahme)
  Zeitstopp  Verkauf zum Schlusskurs des letzten Haltetags
  Kosten     0,3 % je Trade (Gebühren + Spread), Rendite in Euro (Wechselkurseffekt eingerechnet)
"""
from pathlib import Path

import numpy as np
import pandas as pd

DATEN = Path(__file__).resolve().parent / "daten"
HORIZ = 45
KOSTEN = 0.003


def lade():
    df = pd.read_csv(DATEN / "signale.csv.gz", low_memory=False)
    df["datum"] = pd.to_datetime(df["datum"])
    return df


def bereinige(df):
    """Datenfehler entfernen: Nullvolumen, absurde Stürze, Fehlkurse mit sofortiger Gegenbewegung."""
    voll_zurueck = (df["C1"] >= 1 / (1 + df["ret1"]) * 0.98) & (df["ret1"] < -0.08)
    schlecht = (df["vol0"] == 1) | (df["drop"] < -0.6) | voll_zurueck | (df["n_pfad"] < 30)
    return df[~schlecht].copy(), int(schlecht.sum())


def matrizen(df):
    g = lambda p: df[[f"{p}{m}" for m in range(HORIZ + 1)]].to_numpy(float)
    return dict(O=g("O"), H=g("H"), L=g("L"), C=g("C"), X=np.nan_to_num(g("X"), nan=1.0), T=g("T"))


# --------------------------------------------------------------------------
# Einstieg: liefert Index des Einstiegstags (Kauf zur Eröffnung) oder -1
# --------------------------------------------------------------------------
def einstieg(M, regel, param=None):
    O, H, C = M["O"], M["H"], M["C"]
    n = len(O)
    e = np.full(n, -1)
    preis = np.full(n, np.nan)
    if regel == "sofort":                       # nächste Eröffnung nach dem Signal
        e[:] = 1
    elif regel == "warte":                      # param Tage abwarten
        e[:] = param + 1
    elif regel == "umkehr":                     # erster Schluss über dem Vortageshoch innerhalb param Tagen
        for m in range(1, param + 1):
            treffer = (e < 0) & (C[:, m] > H[:, m - 1])
            e[treffer] = m + 1
    elif regel == "umkehr_schluss":             # Kauf direkt zum Schluss des Umkehrtags
        for m in range(1, param + 1):
            treffer = (e < 0) & (C[:, m] > H[:, m - 1])
            e[treffer] = m
            preis[treffer] = C[treffer, m]
        return e, preis, "close"
    elif regel == "limit":                      # Kauflimit param unter dem Signalschluss, 5 Tage gültig
        lim = 1 - param
        for m in range(1, 6):
            treffer = (e < 0) & (M["L"][:, m] <= lim)
            e[treffer] = m
            preis[treffer] = np.minimum(O[treffer, m], lim)
        return e, preis, "limit"
    ok = e > 0
    preis[ok] = O[ok, e[ok]]
    return e, preis, "open"


def handel(M, e, preis, modus, tp, sl, tage):
    """Simuliert Ausstieg. Rückgabe DataFrame je Signal (NaN bei keinem Einstieg)."""
    O, H, L, C, X, T = M["O"], M["H"], M["L"], M["C"], M["X"], M["T"]
    n = len(O)
    ziel = preis * (1 + tp)
    stop = preis * (1 - sl) if sl else np.full(n, -np.inf)
    aus = np.full(n, np.nan)
    aus_tag = np.full(n, -1)
    art = np.full(n, "", dtype=object)
    offen = (e > 0) & np.isfinite(preis)
    for k in range(tage):
        m = e + k
        idx = np.where(offen & (m <= HORIZ))[0]
        if len(idx) == 0:
            break
        mm = m[idx]
        o, h, l, c = O[idx, mm], H[idx, mm], L[idx, mm], C[idx, mm]
        erster = (k == 0)
        if erster and modus == "close":        # Kauf zum Schluss: an diesem Tag nichts mehr
            continue
        if erster and modus == "limit":        # Kauf im Tagesverlauf: nur Stop prüfen (vorsichtig)
            s_hit = l <= stop[idx]
            j = idx[s_hit]
            aus[j], aus_tag[j], art[j] = stop[j], m[j], "SL"
            offen[j] = False
            continue
        # Eröffnung (nicht am Einstiegstag, da Einstieg zur Eröffnung)
        if not erster:
            gap_dn = o <= stop[idx]
            gap_up = (o >= ziel[idx]) & ~gap_dn
            for msk, a in ((gap_dn, "SL"), (gap_up, "TP")):
                j = idx[msk]
                aus[j], aus_tag[j], art[j] = O[j, m[j]], m[j], a
                offen[j] = False
            sel = ~(gap_dn | gap_up)
            idx, mm, h, l, c = idx[sel], mm[sel], h[sel], l[sel], c[sel]
        s_hit = l <= stop[idx]
        t_hit = (h >= ziel[idx]) & ~s_hit
        j = idx[s_hit]
        aus[j], aus_tag[j], art[j] = stop[j], m[j], "SL"
        offen[j] = False
        j = idx[t_hit]
        aus[j], aus_tag[j], art[j] = ziel[j], m[j], "TP"
        offen[j] = False
        if k == tage - 1:
            rest = idx[~s_hit & ~t_hit]
            aus[rest], aus_tag[rest], art[rest] = C[rest, m[rest]], m[rest], "Zeit"
            offen[rest] = False
    rest = np.where(offen)[0]                   # Datenende: zum letzten Schluss
    if len(rest):
        last = np.minimum(e[rest] + tage - 1, HORIZ)
        aus[rest], aus_tag[rest], art[rest] = C[rest, last], last, "Zeit"
    ok = np.isfinite(aus)
    r_lok = np.full(n, np.nan)
    r_eur = np.full(n, np.nan)
    r_lok[ok] = aus[ok] / preis[ok] - 1 - KOSTEN
    xe = X[np.arange(n), np.clip(e, 0, HORIZ)]
    xa = X[np.arange(n), np.clip(aus_tag, 0, HORIZ)]
    r_eur[ok] = (aus[ok] * xa[ok]) / (preis[ok] * xe[ok]) - 1 - KOSTEN
    halte = np.where(ok, aus_tag - e + 1, np.nan)
    tage_e = np.where(ok, T[np.arange(n), np.clip(e, 0, HORIZ)], np.nan)
    tage_a = np.where(ok, T[np.arange(n), np.clip(aus_tag, 0, HORIZ)], np.nan)
    return pd.DataFrame({"gekauft": ok, "art": art, "preis": preis, "r_lok": r_lok, "r": r_eur,
                         "halte": halte, "e_tag": tage_e, "a_tag": tage_a})


def kennzahlen(t, maske=None):
    t = t if maske is None else t[maske]
    t = t[t["gekauft"]]
    n = len(t)
    if n == 0:
        return dict(n=0)
    return dict(n=n,
                treffer=(t["art"] == "TP").mean(),
                stop=(t["art"] == "SL").mean(),
                zeit=(t["art"] == "Zeit").mean(),
                positiv=(t["r"] > 0).mean(),
                ew=t["r"].mean(),
                median=t["r"].median(),
                halte=t["halte"].mean(),
                p5=t["r"].quantile(0.05))


# --------------------------------------------------------------------------
# Depot-Simulation
# --------------------------------------------------------------------------
# EZB-Einlagezins (Näherung für Geldmarkt-ETF/Tagesgeld; negative Zinsen -> 0)
EZB = [("2015-01-01", -0.20), ("2015-12-09", -0.30), ("2016-03-16", -0.40), ("2019-09-18", -0.50),
       ("2022-07-27", 0.00), ("2022-09-14", 0.75), ("2022-11-02", 1.50), ("2022-12-21", 2.00),
       ("2023-02-08", 2.50), ("2023-03-22", 3.00), ("2023-05-10", 3.25), ("2023-06-21", 3.50),
       ("2023-08-02", 3.75), ("2023-09-20", 4.00), ("2024-06-12", 3.75), ("2024-09-18", 3.50),
       ("2024-10-23", 3.25), ("2024-12-18", 3.00), ("2025-02-05", 2.75), ("2025-03-12", 2.50),
       ("2025-04-23", 2.25), ("2025-06-11", 2.00)]


def zins_serie(tage):
    z = pd.Series({pd.Timestamp(d): r for d, r in EZB}).sort_index()
    z = z.reindex(z.index.union(tage)).ffill().reindex(tage)
    return np.maximum(z.values - 0.10, 0) / 100 / 252


def depot(df, t, M, start=10_000, anteil=0.20, max_pos=5, zins=True,
          von="2016-07-01", bis="2026-07-31", max_sektor=None, max_neu=None, prio="kap", seed=0):
    """Tägliche Depotsimulation. df/t/M: Signale, Handelsergebnis, Pfade (gleiche Reihenfolge)."""
    ok = t["gekauft"].to_numpy()
    idx = np.where(ok)[0]
    d0 = df["datum"].to_numpy()
    trades = []
    for i in idx:
        e = int(t["e_tag"].iat[i])
        ein = pd.Timestamp(d0[i]) + pd.Timedelta(days=e)
        aus = pd.Timestamp(d0[i]) + pd.Timedelta(days=int(t["a_tag"].iat[i]))
        trades.append((ein, aus, i))
    tage = pd.bdate_range(von, bis)
    nach_ein = {}
    for ein, aus, i in trades:
        nach_ein.setdefault(ein, []).append((aus, i))
    kap = df["kap_mrd_eur"].fillna(0).to_numpy()
    sektor = df["sektor"].fillna("?").to_numpy()
    zs = zins_serie(tage) if zins else np.zeros(len(tage))
    cash, offen, verlauf, belegung, genommen, verpasst = start, [], [], [], [], 0
    # Für Tagesbewertung: Schlusskurs-Pfad je Trade als Funktion des Datums
    C, X, T = M["C"], M["X"], M["T"]
    e_idx = (t["e_tag"].to_numpy())
    for k, tag in enumerate(tage):
        cash *= 1 + zs[k]
        # Ausstiege vom Vortag oder früher bereits verbucht; heute fällige zuerst
        rest = []
        for p in offen:
            if p["aus"] <= tag and p["ein"] < tag:
                cash += p["wert_exit"]
            else:
                rest.append(p)
        offen = rest
        # Einstiege
        kand = nach_ein.get(tag, [])
        if prio == "kap":
            kand = sorted(kand, key=lambda x: -kap[x[1]])
        elif prio == "zufall":
            kand = [kand[j] for j in np.random.default_rng(seed + k).permutation(len(kand))]
        neu_heute = 0
        for aus, i in kand:
            if max_neu and neu_heute >= max_neu:
                verpasst += 1
                continue
            if len(offen) >= max_pos:
                verpasst += 1
                continue
            if max_sektor and sum(p["sektor"] == sektor[i] for p in offen) >= max_sektor:
                verpasst += 1
                continue
            wert = cash + sum(p["wert"] for p in offen)
            groesse = min(anteil * wert, cash)
            if groesse <= 0:
                verpasst += 1
                continue
            cash -= groesse
            r = t["r"].iat[i]
            offen.append(dict(ein=tag, aus=aus, i=i, groesse=groesse, wert=groesse,
                              wert_exit=groesse * (1 + r), sektor=sektor[i]))
            genommen.append(i)
            neu_heute += 1
        # Tagesbewertung + Ausstiege am Einstiegstag
        rest = []
        for p in offen:
            if p["aus"] <= tag and p["ein"] == tag:
                cash += p["wert_exit"]
                continue
            i = p["i"]
            off = (tag - pd.Timestamp(d0[i])).days
            m = np.searchsorted(T[i], off, side="right") - 1
            m = int(np.clip(m, 0, HORIZ))
            e = int(np.searchsorted(T[i], e_idx[i]))
            p["wert"] = p["groesse"] * (C[i, m] * X[i, m]) / (t["preis"].iat[i] * X[i, e])
            rest.append(p)
        offen = rest
        verlauf.append(cash + sum(p["wert"] for p in offen))
        belegung.append(len(offen))
    v = pd.Series(verlauf, index=tage)
    b = pd.Series(belegung, index=tage)
    return v, b, genommen, verpasst


def depot_kennzahlen(v, b):
    jahre = (v.index[-1] - v.index[0]).days / 365.25
    dd = v / v.cummax() - 1
    j = v.resample("YE").last()
    j = pd.concat([pd.Series([v.iloc[0]], index=[v.index[0]]), j])
    jr = j.pct_change().dropna()
    jr.index = jr.index.year
    return dict(ende=v.iloc[-1], cagr=(v.iloc[-1] / v.iloc[0]) ** (1 / jahre) - 1,
                max_dd=dd.min(), pos_mittel=b.mean(), cash_tage=(b == 0).mean(),
                jahre=jr.round(4).to_dict())
