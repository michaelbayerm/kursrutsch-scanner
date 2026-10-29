"""
Regel A mit Call-Optionsscheinen statt Aktien.

Ziel (+tp) und Stop (−sl) gelten weiter auf den Aktienkurs. Der Optionsschein wird mit
Black-Scholes bewertet:
  - Laufzeit beim Kauf LZ Tage, Basispreis so gewählt, dass der Hebel (Omega) beim Kauf
    dem Zielhebel entspricht
  - implizite Vola beim Kauf = VIX × IV_FAKTOR (Einzelaktien sind schwankungsreicher als
    der Index); danach bewegt sie sich im Verhältnis des VIX (vega-Effekt) oder bleibt
    konstant (zum Vergleich)
  - Zeitwertverlust über die tatsächliche Haltedauer in Kalendertagen
  - Spread SPREAD (Round-Trip, in % des Scheinpreises), keine Dividendenanpassung
"""
import math
from pathlib import Path

import numpy as np
import pandas as pd

import analyse as a

R_ZINS = 0.03
IV_FAKTOR = 1.3
SPREAD = 0.015


def ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(x / math.sqrt(2)))


def bs_call(S, K, T, vol, r=R_ZINS):
    T = np.maximum(T, 1e-6)
    d1 = (np.log(S / K) + (r + 0.5 * vol ** 2) * T) / (vol * np.sqrt(T))
    d2 = d1 - vol * np.sqrt(T)
    c = S * ncdf(d1) - K * np.exp(-r * T) * ncdf(d2)
    return np.maximum(c, 0.0), ncdf(d1)


def basis_fuer_hebel(S, T, vol, ziel):
    """Basispreis, bei dem Omega = Delta·S/C dem Zielhebel entspricht (Bisektion)."""
    lo, hi = np.full_like(S, 0.3 * S), np.full_like(S, 1.5 * S)
    for _ in range(60):
        mid = (lo + hi) / 2
        c, d = bs_call(S, mid, T, vol)
        om = d * S / np.maximum(c, 1e-12)
        zu_hoch = om > ziel            # höherer Basispreis -> höherer Hebel
        hi = np.where(zu_hoch, mid, hi)
        lo = np.where(zu_hoch, lo, mid)
    return (lo + hi) / 2


def schein_rendite(df, t, M, vix, hebel, lz=120, vix_folgt=True):
    ok = t["gekauft"].to_numpy()
    i = np.where(ok)[0]
    d0 = df["datum"].to_numpy()
    e_tag = t["e_tag"].to_numpy()[i]
    a_tag = t["a_tag"].to_numpy()[i]
    datum_e = pd.to_datetime(d0[i]) + pd.to_timedelta(e_tag, unit="D")
    datum_a = pd.to_datetime(d0[i]) + pd.to_timedelta(a_tag, unit="D")
    v = vix.sort_index()
    pos = lambda d: v.iloc[np.clip(v.index.searchsorted(d, side="right") - 1, 0, None)].to_numpy()
    vix_e = pos(datum_e - pd.Timedelta(days=1))           # Vorabend des Kaufs
    vix_a = pos(datum_a)
    iv_e = vix_e / 100 * IV_FAKTOR
    iv_a = iv_e * (vix_a / vix_e) if vix_folgt else iv_e
    S_e = t["preis"].to_numpy()[i]
    T_e = lz / 365
    K = basis_fuer_hebel(S_e, np.full_like(S_e, T_e), iv_e, hebel)
    c_e, _ = bs_call(S_e, K, T_e, iv_e)
    S_a = S_e * (1 + t["r_lok"].to_numpy()[i] + a.KOSTEN)  # Aktien-Ausstiegskurs (ohne Aktienkosten)
    T_a = T_e - (a_tag - e_tag) / 365
    c_a, _ = bs_call(S_a, K, T_a, iv_a)
    r = c_a / c_e - 1 - SPREAD
    out = np.full(len(t), np.nan)
    out[i] = np.maximum(r, -1.0)
    return out


def main():
    df, _ = a.bereinige(a.lade())
    df = df.reset_index(drop=True)
    M = a.matrizen(df)
    f = lambda s: s.fillna(False).to_numpy()
    A = f(df.kap_mrd_eur >= 5) & f(df.vix.between(25, 40)) & f(df.region == "USA") & f(df.fall_tage >= 2)
    vix = pd.read_csv(a.DATEN / "indizes.csv.gz", index_col=0, parse_dates=True)["^VIX"].dropna()
    e, p, mo = a.einstieg(M, "warte", 2)
    zeilen, depot = [], []
    for tp, sl in [(0.04, 0.07), (0.04, 0.10)]:
        t = a.handel(M, e, p, mo, tp, sl, 20)
        idx = np.where(A)[0]
        d, tt, mm = df.iloc[idx].reset_index(drop=True), t.iloc[idx].reset_index(drop=True), {k: v[idx] for k, v in M.items()}
        gek = tt.gekauft.to_numpy()
        tr = (tt.art == "TP").to_numpy()
        zeilen.append(dict(ziel=tp, stop=sl, instrument="Aktie", hebel=1, vola="–", treffer=tr[gek].mean(),
                           ew=np.nanmean(tt.r[gek]), gewinn_mittel=np.nanmean(tt.r[gek & tr]),
                           verlust_mittel=np.nanmean(tt.r[gek & ~tr]), schlechtester=np.nanmin(tt.r[gek])))
        for h in [3.5, 4.5, 6.0]:
            for folgt in [True, False]:
                rw = schein_rendite(d, tt, mm, vix, h, vix_folgt=folgt)
                zeilen.append(dict(ziel=tp, stop=sl, instrument="Optionsschein", hebel=h,
                                   vola="folgt VIX" if folgt else "konstant", treffer=tr[gek].mean(),
                                   positiv=np.mean(rw[gek] > 0), ew=np.nanmean(rw[gek]),
                                   gewinn_mittel=np.nanmean(rw[gek & tr]), verlust_mittel=np.nanmean(rw[gek & ~tr]),
                                   schlechtester=np.nanmin(rw[gek])))
                if folgt:
                    tw = tt.copy()
                    tw["r"] = rw
                    for anteil, mx in [(0.20, 5), (0.10, 5), (0.05, 5)]:
                        v_, b_, gen, verp = a.depot(d, tw, mm, anteil=anteil, max_pos=mx, hebel=h)
                        k = a.depot_kennzahlen(v_, b_)
                        depot.append(dict(ziel=tp, stop=sl, hebel=h, anteil=anteil, ende=round(k["ende"]),
                                          cagr=k["cagr"], max_dd=k["max_dd"], trades=len(gen),
                                          ew=np.nanmean(rw[gen]) if len(gen) else np.nan,
                                          schlechtestes_jahr=min(k["jahre"].values())))
            # Aktien-Depot zum Vergleich
        v_, b_, gen, _ = a.depot(d, tt, mm, anteil=0.2, max_pos=5)
        k = a.depot_kennzahlen(v_, b_)
        depot.append(dict(ziel=tp, stop=sl, hebel=1, anteil=0.2, ende=round(k["ende"]), cagr=k["cagr"],
                          max_dd=k["max_dd"], trades=len(gen), ew=np.nanmean(tt.r.to_numpy()[gen]),
                          schlechtestes_jahr=min(k["jahre"].values())))
    Z, D = pd.DataFrame(zeilen), pd.DataFrame(depot)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 20)
    print(Z.round(4).to_string())
    print(D.round(4).to_string())
    Z.to_csv(Path(__file__).parent / "optionsscheine_trades.csv", index=False, float_format="%.4f")
    D.to_csv(Path(__file__).parent / "optionsscheine_depot.csv", index=False, float_format="%.4f")


if __name__ == "__main__":
    main()
