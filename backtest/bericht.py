"""
Erzeugt backtest/ergebnisse.json mit allen Kennzahlen für den Bericht.
Aufruf: python backtest/bericht.py   (nach daten_export.py)
"""
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

import analyse as a

AUS = Path(__file__).resolve().parent / "ergebnisse.json"
TP, SL, TAGE = 0.04, 0.07, 20


def kz(t, m):
    k = a.kennzahlen(t, m)
    return {key: (round(float(v), 4) if isinstance(v, (float, np.floating)) else int(v)) for key, v in k.items()}


def main():
    roh = a.lade()
    df, n_raus = a.bereinige(roh)
    df = df.reset_index(drop=True)
    M = a.matrizen(df)
    f = lambda s: s.fillna(False).to_numpy()
    gross = f(df.kap_mrd_eur >= 5)
    IS = (df.datum < "2022-01-01").to_numpy()
    R = {"daten": dict(signale_roh=len(roh), entfernt_datenfehler=n_raus, signale=len(df),
                       signale_5mrd=int(gross.sum()),
                       je_region=df[gross].region.value_counts().to_dict(),
                       je_jahr=df[gross].groupby(df.datum.dt.year).size().to_dict(),
                       zeitraum="01.07.2016–30.06.2026")}

    regelA = gross & f(df.vix.between(25, 40)) & f(df.region == "USA") & f(df.fall_tage >= 2)
    R["regelA_n"] = int(regelA.sum())

    # 1) Einstiegsvarianten
    einst = {"Sofort (nächste Eröffnung)": ("sofort", None), "1 Tag warten": ("warte", 1),
             "2 Tage warten": ("warte", 2), "3 Tage warten": ("warte", 3),
             "Umkehrtag (Schluss > Vortageshoch)": ("umkehr", 5), "Limit 3 % unter Signalkurs": ("limit", 0.03)}
    R["einstieg"] = []
    T = {}
    for name, (regel, pa) in einst.items():
        e, p, mo = a.einstieg(M, regel, pa)
        t = a.handel(M, e, p, mo, TP, SL, TAGE)
        T[name] = t
        R["einstieg"].append(dict(name=name, alle=kz(t, gross), regelA=kz(t, regelA)))

    # 2) Merkmale (Einstieg sofort und 2 Tage warten, Universum > 5 Mrd.)
    t0, t2 = T["Sofort (nächste Eröffnung)"], T["2 Tage warten"]
    merkmale = {
        "Marktkap. (Mrd. €)": ("kap_mrd_eur", [5, 10, 20, 50, 100, 1e6], ["5–10", "10–20", "20–50", "50–100", "> 100"]),
        "Absturz in 1–3 Tagen": ("drop", [-1, -0.25, -0.2, -0.15, -0.125, -0.1], ["> 25 %", "20–25 %", "15–20 %", "12,5–15 %", "10–12,5 %"]),
        "Absturz verteilt auf": ("fall_tage", [0, 1, 2, 3], ["1 Tag", "2 Tage", "3 Tage"]),
        "VIX am Signaltag": ("vix", [0, 15, 20, 25, 30, 40, 100], ["< 15", "15–20", "20–25", "25–30", "30–40", "> 40"]),
        "Markt im selben Zeitraum": ("markt_ret", [-1, -0.05, -0.01, 0.01, 1], ["< −5 %", "−5 bis −1 %", "±1 %", "> +1 %"]),
        "Abstand zum 200-Tage-Schnitt": ("sma200", [-1, -0.1, 0, 0.1, 0.2, 10], ["< −10 %", "−10–0 %", "0–10 %", "10–20 %", "> 20 %"]),
    }
    R["merkmale"] = {}
    for titel, (col, bins, labels) in merkmale.items():
        g = pd.cut(df[col], bins, labels=labels)
        R["merkmale"][titel] = [dict(gruppe=str(l), sofort=kz(t0, gross & (g == l).to_numpy()),
                                     warte2=kz(t2, gross & (g == l).to_numpy())) for l in labels]
    R["merkmale"]["Region"] = [dict(gruppe=r, sofort=kz(t0, gross & f(df.region == r)),
                                    warte2=kz(t2, gross & f(df.region == r))) for r in ["USA", "Europa", "Japan"]]
    R["merkmale"]["Quartalszahlen als Auslöser"] = [
        dict(gruppe=g, sofort=kz(t0, gross & m), warte2=kz(t2, gross & m))
        for g, m in [("ja", f(df.earnings == 1)), ("nein", f(df.earnings == 0))]]
    R["merkmale"]["Jahr"] = [dict(gruppe=str(y), sofort=kz(t0, gross & f(df.datum.dt.year == y)),
                                  warte2=kz(t2, gross & f(df.datum.dt.year == y))) for y in range(2016, 2027)]

    # 3) Overfitting-Test: alle Filterkombinationen (bis 4) in 2016–2021 vs. 2022–2026
    C = {
        "VIX 20–40": df.vix.between(20, 40), "VIX 25–40": df.vix.between(25, 40),
        "Markt mitgefallen": df.markt_ret < -0.01, "Trend 12 M > 20 %": df.ret12m_vor > 0.2,
        "über 200-Tage-Linie": df.sma200 > 0, "Absturz 2–3 Tage": df.fall_tage >= 2,
        "kein Einzeltag < −5 %": df.ret1 > -0.05, "USA": df.region == "USA",
        "Volumen < 3×": df.vol_ratio < 3, "Kap. ≥ 20 Mrd.": df.kap_mrd_eur >= 20,
        "keine Quartalszahlen": df.earnings == 0, "Absturz < 20 %": df["drop"] > -0.2,
    }
    C = {k: f(v) for k, v in C.items()}
    gek, tp_, r_ = t2.gekauft.to_numpy(), (t2.art == "TP").to_numpy(), t2.r.to_numpy()
    rows = []
    for k in range(1, 5):
        for combo in itertools.combinations(C, k):
            m = gross & np.logical_and.reduce([C[c] for c in combo]) & gek
            mi, mo_ = m & IS, m & ~IS
            if mi.sum() >= 120 and mo_.sum() >= 120:
                rows.append(dict(filter=" + ".join(combo), n_is=int(mi.sum()), tr_is=tp_[mi].mean(),
                                 ew_is=np.nanmean(r_[mi]), n_oos=int(mo_.sum()), tr_oos=tp_[mo_].mean(),
                                 ew_oos=np.nanmean(r_[mo_])))
    E = pd.DataFrame(rows)
    E.to_csv(Path(__file__).resolve().parent / "kombinationen.csv", index=False, float_format="%.4f")
    top_is = E.sort_values("tr_is", ascending=False).head(10)
    R["overfitting"] = dict(
        kombinationen=len(E), korrelation=round(float(E[["tr_is", "tr_oos"]].corr().iloc[0, 1]), 3),
        top10_is_tr_is=round(float(top_is.tr_is.mean()), 4), top10_is_tr_oos=round(float(top_is.tr_oos.mean()), 4),
        anteil_beide_75=round(float(((E.tr_is >= 0.75) & (E.tr_oos >= 0.75)).mean()), 4),
        beispiele=top_is.head(5).round(4).to_dict("records"),
        punkte=E[["tr_is", "tr_oos"]].round(3).values.tolist())
    R["regelA_is_oos"] = dict(is_=kz(t2, regelA & IS), oos=kz(t2, regelA & ~IS))

    # 4) TP/SL-Raster für Regel A (2 Tage warten)
    e, p, mo = a.einstieg(M, "warte", 2)
    grid = []
    for tp in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
        for sl in [0.05, 0.07, 0.10, 0.15]:
            t = a.handel(M, e, p, mo, tp, sl, TAGE)
            ka, kb = kz(t, regelA), kz(t, gross)
            grid.append(dict(tp=tp, sl=sl, A=ka, alle=kb,
                             A_is=kz(t, regelA & IS)["treffer"], A_oos=kz(t, regelA & ~IS)["treffer"]))
    R["raster"] = grid

    # 5) Depot-Simulation 10.000 €
    bench = pd.read_csv(a.DATEN / "indizes.csv.gz", index_col=0, parse_dates=True)
    welt = bench["EUNL.DE"].dropna()
    tage = pd.bdate_range("2016-07-01", "2026-07-31")
    welt = welt.reindex(welt.index.union(tage)).ffill().reindex(tage)
    welt = 10_000 * welt / welt.iloc[0]
    zins = a.zins_serie(tage)
    cash = pd.Series(10_000 * np.cumprod(1 + zins), index=tage)

    def sim(maske, regel, pa, tp, sl, anteil=0.2, max_pos=5, max_neu=None):
        ee = a.einstieg(M, regel, pa)
        t = a.handel(M, *ee, tp, sl, TAGE)
        idx = np.where(maske)[0]
        d, tt, mm = df.iloc[idx].reset_index(drop=True), t.iloc[idx].reset_index(drop=True), {k: v[idx] for k, v in M.items()}
        v, b, gen, verp = a.depot(d, tt, mm, anteil=anteil, max_pos=max_pos, max_neu=max_neu)
        k = a.depot_kennzahlen(v, b)
        g = tt.iloc[gen]
        k.update(trades=len(gen), trades_jahr=round(len(gen) / 10, 1), treffer=round(float((g.art == "TP").mean()), 4),
                 ew_trade=round(float(g.r.mean()), 4), verpasst=verp, halte=round(float(g.halte.mean()), 2),
                 ende=round(k["ende"]), cagr=round(k["cagr"], 4), max_dd=round(k["max_dd"], 4),
                 pos_mittel=round(k["pos_mittel"], 2), cash_tage=round(k["cash_tage"], 4),
                 tage_investiert=round(float((b > 0).mean()), 4))
        return k, v, b

    varianten = {
        "Scanner-Regel pur (sofort, +4/−7)": (gross, "sofort", None, 0.04, 0.07),
        "Regel A (+4/−7)": (regelA, "warte", 2, 0.04, 0.07),
        "Regel A (+3/−7)": (regelA, "warte", 2, 0.03, 0.07),
        "Regel A (+4/−10)": (regelA, "warte", 2, 0.04, 0.10),
        "Regel A (+5/−10)": (regelA, "warte", 2, 0.05, 0.10),
    }
    R["depot"], kurven = [], {}
    for name, (mk, regel, pa, tp, sl) in varianten.items():
        for anteil, mx in [(0.2, 5), (0.25, 4), (0.33, 3)]:
            k, v, b = sim(mk, regel, pa, tp, sl, anteil, mx)
            k.update(name=name, anteil=anteil, max_pos=mx)
            R["depot"].append(k)
            if anteil == 0.2:
                kurven[name] = v
                if name == "Regel A (+4/−7)":
                    R["belegung"] = {str(n): round(float((b == n).mean()), 4) for n in range(6)}
    # Monatswerte der Kurven fürs Diagramm
    kurven["MSCI World (iShares EUNL)"] = welt
    kurven["Nur Geldmarkt"] = cash
    R["kurven"] = {n: [[d.strftime("%Y-%m"), round(float(x))] for d, x in s.resample("ME").last().items()]
                   for n, s in kurven.items()}
    jw = welt.iloc[-1] / welt.iloc[0]
    R["benchmark"] = dict(welt_ende=round(float(welt.iloc[-1])), welt_cagr=round(float(jw ** (1 / 10.08) - 1), 4),
                          welt_dd=round(float((welt / welt.cummax() - 1).min()), 4),
                          cash_ende=round(float(cash.iloc[-1])))

    AUS.write_text(json.dumps(R, ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
    print("geschrieben:", AUS)


if __name__ == "__main__":
    main()
