"""
Backtest-Datenexport für den Kursrutsch-Scanner.

Lädt Tageskurse 2015–2026 für die Indexmitglieder (USA, Europa, Japan), findet alle
Signale "−10 % gegenüber dem höchsten Schlusskurs der letzten 3 Tage" (wie der Scanner)
zwischen 01.07.2016 und 30.06.2026 und exportiert je Signal Merkmale und den
Kursverlauf der folgenden 45 Handelstage. Die eigentliche Auswertung (Einstieg,
Ausstieg, Depot) läuft danach separat auf diesen Daten.

Ausgabe in backtest/daten/:
  signale.csv.gz   ein Signal je Zeile (Merkmale + Pfade O/H/L/C, FX, Tagesabstände)
  indizes.csv.gz   Schlusskurse der Vergleichsindizes / Benchmarks
  meta.json        Abdeckung, Fehler, Zählungen
"""
import json
import time
import concurrent.futures as cf
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "backtest" / "daten"
OUT.mkdir(parents=True, exist_ok=True)

START_DL, END_DL = "2015-01-01", "2026-09-30"
SIG_START, SIG_END = pd.Timestamp("2016-07-01"), pd.Timestamp("2026-06-30")
SCHWELLE = -0.10          # wie Scanner: −10 % ggü. höchstem Schluss der letzten 3 Tage
FENSTER = 3
SPERRE = 20               # gleicher Titel frühestens 20 Handelstage nach dem letzten Signal wieder
HORIZ = 45                # exportierte Handelstage nach dem Signaltag

REGION = {
    "S&P 500": "USA", "Nasdaq-100": "USA",
    "DAX": "Europa", "MDAX": "Europa", "Euro Stoxx 50": "Europa", "FTSE 100": "Europa",
    "CAC 40": "Europa", "SMI": "Europa", "AEX": "Europa", "IBEX 35": "Europa", "FTSE MIB": "Europa",
    "Nikkei 225": "Japan",
}
REGIONSINDEX = {"USA": "^GSPC", "Europa": "^STOXX", "Japan": "^N225"}
EXTRA_INDIZES = ["^GSPC", "^STOXX", "^STOXX50E", "^N225", "^GDAXI", "^VIX", "EUNL.DE", "URTH"]
SUB_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100)}
UA = {"User-Agent": "Mozilla/5.0 (backtest kursrutsch-scanner)"}

meta = {"fehler": []}


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------
# Universum
# --------------------------------------------------------------------------
def universum():
    data = json.loads((ROOT / "cache" / "indizes.json").read_text())["data"]
    tick = {}
    for idx, lst in data.items():
        if idx not in REGION:
            continue
        for t in lst:
            e = tick.setdefault(t, {"indizes": [], "region": REGION[idx], "entfernt": 0})
            e["indizes"].append(idx)
    # Survivorship-Check: seit 2016 aus dem S&P 500 entfernte Titel (soweit Yahoo noch Daten hat)
    entfernt = []
    try:
        html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                            headers=UA, timeout=30).text
        tabs = pd.read_html(StringIO(html))
        def flach(t):
            t = t.copy()
            t.columns = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
            return t
        tabs = [flach(t) for t in tabs]
        ch = next(t for t in tabs if any("Removed" in c for c in t.columns))
        dcol = next(c for c in ch.columns if "date" in c.lower())
        rcol = next(c for c in ch.columns if "Removed" in c and ("Ticker" in c or "Symbol" in c))
        ch["_d"] = pd.to_datetime(ch[dcol], errors="coerce")
        for _, r in ch[ch["_d"] >= "2016-07-01"].iterrows():
            t = str(r[rcol]).strip().replace(".", "-")
            if t and t != "nan" and t not in tick:
                entfernt.append(t)
    except Exception as e:
        meta["fehler"].append(f"Wikipedia S&P-Änderungen: {type(e).__name__} {e}")
    for t in sorted(set(entfernt)):
        tick[t] = {"indizes": ["S&P 500 (entfernt)"], "region": "USA", "entfernt": 1}
    meta["universum"] = len(tick)
    meta["entfernte_sp500_kandidaten"] = len(set(entfernt))
    return tick


# --------------------------------------------------------------------------
# Kurse
# --------------------------------------------------------------------------
def lade_kurse(tickers, chunk=60):
    felder = {f: [] for f in ["Open", "High", "Low", "Close", "Adj Close", "Volume"]}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = None
        for versuch in range(4):
            try:
                df = yf.download(part, start=START_DL, end=END_DL, interval="1d",
                                 auto_adjust=False, progress=False, threads=True,
                                 group_by="column", multi_level_index=True)
                break
            except Exception as e:
                log("  Download-Fehler, neuer Versuch:", e)
                time.sleep(15 * (versuch + 1))
        if df is None or df.empty:
            continue
        for f in felder:
            if f in df.columns.get_level_values(0):
                felder[f].append(df[f])
        log(f"  Kurse {i + len(part)}/{len(tickers)}")
    out = {}
    for f, v in felder.items():
        x = pd.concat(v, axis=1)
        x = x.loc[:, ~x.columns.duplicated()]
        if getattr(x.index, "tz", None) is not None:
            x.index = x.index.tz_localize(None)
        out[f] = x.sort_index()
    return out


def lade_serien(symbole):
    df = yf.download(symbole, start=START_DL, end=END_DL, interval="1d",
                     auto_adjust=True, progress=False, threads=True,
                     group_by="column", multi_level_index=True)["Close"]
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df.sort_index()


# --------------------------------------------------------------------------
# Stammdaten: Marktkapitalisierung heute, Währung, Sektor, Quartalszahlen-Termine
# --------------------------------------------------------------------------
def stammdaten(t):
    r = {"ticker": t}
    tk = yf.Ticker(t)
    for versuch in range(3):
        try:
            fi = tk.fast_info
            r["kap"], r["waehrung"], r["kurs_jetzt"] = fi.market_cap, fi.currency, fi.last_price
            break
        except Exception as e:
            r["fehler_fast"] = str(e)[:80]
            time.sleep(3 * (versuch + 1))
    for versuch in range(3):
        try:
            info = tk.info or {}
            r["name"] = info.get("shortName") or info.get("longName")
            r["sektor"] = info.get("sector")
            r["branche"] = info.get("industry")
            r["land"] = info.get("country")
            if not r.get("kap"):
                r["kap"] = info.get("marketCap")
                r["kurs_jetzt"] = info.get("regularMarketPrice") or info.get("previousClose")
            r["waehrung"] = r.get("waehrung") or info.get("currency")
            break
        except Exception as e:
            r["fehler_info"] = str(e)[:80]
            time.sleep(3 * (versuch + 1))
    termine = []
    for versuch in range(3):
        try:
            ed = tk.get_earnings_dates(limit=60)
            if ed is not None and len(ed):
                for ts in ed.index:
                    ts = pd.Timestamp(ts)
                    # Meldung ab Mittag Ortszeit -> Kursreaktion erst am nächsten Handelstag
                    termine.append((ts.strftime("%Y-%m-%d"), int(ts.hour >= 12)))
            break
        except Exception as e:
            r["fehler_earn"] = str(e)[:80]
            time.sleep(5 * (versuch + 1))
    r["termine"] = termine
    return r


def alle_stammdaten(tickers):
    cache_datei = OUT / "stamm.json"
    res = json.loads(cache_datei.read_text()) if cache_datei.exists() else {}
    def fehlt(t):
        r = res.get(t)
        return r is None or not r.get("kap") or not r.get("sektor") or not r.get("termine")
    for durchgang, (worker, pause) in enumerate([(3, 0.3), (1, 2.0), (1, 6.0)], 1):
        offen = [t for t in tickers if fehlt(t)]
        log(f"  Stammdaten Durchgang {durchgang}: {len(offen)} Titel")
        if not offen:
            break
        def hole(t):
            time.sleep(pause)
            neu = stammdaten(t)
            alt = res.get(t, {})
            for k2, v in alt.items():          # vorhandene Werte nicht verlieren
                if v and not neu.get(k2):
                    neu[k2] = v
            return neu
        with cf.ThreadPoolExecutor(max_workers=worker) as ex:
            for n, r in enumerate(ex.map(hole, offen), 1):
                res[r["ticker"]] = r
                if n % 100 == 0:
                    log(f"    {n}/{len(offen)}")
        cache_datei.write_text(json.dumps(res, ensure_ascii=False))
    return res


# --------------------------------------------------------------------------
# Signale
# --------------------------------------------------------------------------
def asof(serie, datum):
    """Letzter Wert der Serie am oder vor 'datum' (NaN, wenn keiner)."""
    if serie is None or serie.empty:
        return np.nan
    i = serie.index.searchsorted(datum, side="right") - 1
    return serie.iloc[i] if i >= 0 else np.nan


def reaktionstage(termine, daten):
    """Quartalszahlen-Termine -> Handelstag, an dem der Kurs darauf reagiert."""
    out = []
    for d, nachmittag in termine:
        d = pd.Timestamp(d)
        i = daten.searchsorted(d, side="right" if nachmittag else "left")
        if i < len(daten):
            out.append(daten[i])
    return sorted(set(out))


def signale_fuer(t, o, h, l, c, adj, v, stamm, info, fx, idx, vix):
    s = pd.DataFrame({"o": o, "h": h, "l": l, "c": c, "adj": adj, "v": v}).dropna(subset=["c", "adj"])
    s = s[s["c"] > 0]
    if len(s) < 260:
        return []
    f = (s["adj"] / s["c"]).values                       # Dividendenanpassung
    O, H, L, C = s["o"].values * f, s["h"].values * f, s["l"].values * f, s["adj"].values
    Craw = s["c"].values
    V = s["v"].fillna(0).values
    daten = s.index
    n = len(s)

    waehr = stamm.get("waehrung")
    teiler = 1
    if waehr in SUB_UNITS:
        waehr, teiler = SUB_UNITS[waehr]
    kap = stamm.get("kap")
    kurs_jetzt = stamm.get("kurs_jetzt")
    fxs = fx.get(waehr) if waehr and waehr != "EUR" else None
    region = info["region"]
    ix = idx.get(REGIONSINDEX[region])
    reakt = set(reaktionstage(stamm.get("termine", []), daten))
    reakt_liste = sorted(reakt)

    ev, letzte = [], -10**9
    for i in range(FENSTER, n):
        d = daten[i]
        if d < SIG_START or d > SIG_END:
            continue
        fenster = C[i - FENSTER:i]
        mx = fenster.max()
        drop = C[i] / mx - 1
        if drop > SCHWELLE or i - letzte < SPERRE:
            continue
        letzte = i
        j = i - FENSTER + int(fenster.argmax())          # Tag des Hochs vor dem Absturz
        r = {"ticker": t, "datum": d.strftime("%Y-%m-%d"), "region": region,
             "indizes": "|".join(info["indizes"]), "entfernt": info["entfernt"],
             "sektor": stamm.get("sektor"), "branche": stamm.get("branche"),
             "name": stamm.get("name"), "waehrung": waehr}
        r["drop"] = drop
        r["fall_tage"] = i - j
        r["ret1"] = C[i] / C[i - 1] - 1
        r["gap"] = O[i] / C[i - 1] - 1 if O[i] > 0 else np.nan
        r["schluss_pos"] = (C[i] - L[i]) / (H[i] - L[i]) if H[i] > L[i] else np.nan
        r["vol_ratio"] = V[i] / V[max(0, i - 50):i].mean() if V[max(0, i - 50):i].mean() > 0 else np.nan
        r["vol0"] = int(V[i] == 0)
        r["sma200"] = C[j] / C[j - 199:j + 1].mean() - 1 if j >= 199 else np.nan
        r["ret3m_vor"] = C[j] / C[j - 63] - 1 if j >= 63 else np.nan
        r["ret12m_vor"] = C[j] / C[j - 252] - 1 if j >= 252 else np.nan
        r["abst_52wh"] = C[j] / C[max(0, j - 251):j + 1].max() - 1
        lr = np.diff(np.log(C[max(0, j - 60):j + 1]))
        r["vola60_vor"] = lr.std() * np.sqrt(252) if len(lr) > 10 else np.nan
        # Marktkapitalisierung am Tag des Hochs, in Mrd. € (heutige Aktienzahl × damaliger Kurs)
        if kap and kurs_jetzt and kurs_jetzt > 0:
            kap_lokal = kap * Craw[j] / kurs_jetzt / teiler
            rate = asof(fxs, daten[j]) if fxs is not None else 1.0
            r["kap_mrd_eur"] = kap_lokal / rate / 1e9 if rate and rate > 0 else np.nan
        else:
            r["kap_mrd_eur"] = np.nan
        # Quartalszahlen als Auslöser?
        r["earnings"] = int(any(daten[j] < x <= d for x in reakt))
        k = np.searchsorted(np.array(reakt_liste, dtype="datetime64[ns]"), np.datetime64(d), side="right") - 1
        r["tage_seit_earn"] = (d - reakt_liste[k]).days if k >= 0 else np.nan
        r["earn_daten"] = int(len(reakt_liste) > 0)
        # Markt im selben Zeitraum
        if ix is not None:
            a, b = asof(ix, daten[j]), asof(ix, d)
            r["markt_ret"] = b / a - 1 if a and a > 0 else np.nan
            r["markt_sma200"] = b / ix.loc[:d].iloc[-200:].mean() - 1 if len(ix.loc[:d]) >= 200 else np.nan
        r["vix"] = asof(vix, d) if vix is not None else np.nan
        # Pfad: Tag 0 = Signaltag, normiert auf dessen Schlusskurs
        r["n_pfad"] = min(HORIZ, n - 1 - i)
        for m in range(HORIZ + 1):
            k2 = i + m
            if k2 < n:
                r[f"O{m}"], r[f"H{m}"] = O[k2] / C[i], H[k2] / C[i]
                r[f"L{m}"], r[f"C{m}"] = L[k2] / C[i], C[k2] / C[i]
                r[f"T{m}"] = (daten[k2] - d).days
                if fxs is not None:
                    a, b = asof(fxs, d), asof(fxs, daten[k2])
                    r[f"X{m}"] = a / b if b and b > 0 else np.nan     # Faktor lokal -> EUR
                else:
                    r[f"X{m}"] = 1.0
        ev.append(r)
    return ev


def main():
    t0 = time.time()
    uni = universum()
    tickers = sorted(uni)
    log(f"Universum: {len(tickers)} Titel")

    log("Kurse laden …")
    k = lade_kurse(tickers)
    mit_daten = [t for t in tickers if t in k["Close"].columns and k["Close"][t].notna().sum() > 260]
    meta["mit_kursdaten"] = len(mit_daten)
    meta["entfernt_mit_kursdaten"] = sum(uni[t]["entfernt"] for t in mit_daten)
    log(f"  mit Kursdaten: {len(mit_daten)}")

    log("Indizes & Wechselkurse laden …")
    idx_df = lade_serien(EXTRA_INDIZES)
    idx = {c: idx_df[c].dropna() for c in idx_df.columns}
    vix = idx.get("^VIX")

    log("Stammdaten laden …")
    stamm = alle_stammdaten(mit_daten)
    waehrungen = set()
    for r in stamm.values():
        w = r.get("waehrung")
        if w:
            waehrungen.add(SUB_UNITS.get(w, (w, 1))[0])
    fx_sym = [f"EUR{w}=X" for w in sorted(waehrungen) if w != "EUR"]
    fx_df = lade_serien(fx_sym) if fx_sym else pd.DataFrame()
    fx = {s[3:6]: fx_df[s].dropna() for s in fx_df.columns}
    meta["waehrungen"] = sorted(waehrungen)
    meta["stamm_ohne_kap"] = sum(1 for r in stamm.values() if not r.get("kap"))
    meta["stamm_ohne_termine"] = sum(1 for r in stamm.values() if not r.get("termine"))
    meta["stamm_ohne_sektor"] = sum(1 for r in stamm.values() if not r.get("sektor"))

    log("Signale suchen …")
    alle = []
    for n, t in enumerate(mit_daten, 1):
        try:
            alle += signale_fuer(t, k["Open"][t], k["High"][t], k["Low"][t], k["Close"][t],
                                 k["Adj Close"][t], k["Volume"][t], stamm.get(t, {}), uni[t],
                                 fx, idx, vix)
        except Exception as e:
            meta["fehler"].append(f"{t}: {e}")
        if n % 200 == 0:
            log(f"  {n}/{len(mit_daten)} Titel, {len(alle)} Signale")
    df = pd.DataFrame(alle)
    meta["signale"] = len(df)
    df.to_csv(OUT / "signale.csv.gz", index=False, float_format="%.5g", compression="gzip")

    bench = pd.concat({**{c: idx[c] for c in idx}, **{f"FX_{c}": fx[c] for c in fx}}, axis=1)
    bench.to_csv(OUT / "indizes.csv.gz", float_format="%.6g", compression="gzip")

    meta["laufzeit_min"] = round((time.time() - t0) / 60, 1)
    meta["fehler"] = meta["fehler"][:200]
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    log(json.dumps({k2: v for k2, v in meta.items() if k2 != "fehler"}, ensure_ascii=False))
    log(f"Fertig: {len(df)} Signale")


if __name__ == "__main__":
    main()
