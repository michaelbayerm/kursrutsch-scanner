#!/usr/bin/env python3
"""
Aktien-Crash-Scanner
====================
Durchsucht große Indizes weltweit nach Aktien, die in den letzten 1–3
Handelstagen um mehr als X % gefallen sind (Standard 10 %), mit einer
Marktkapitalisierung über Y Mrd. EUR (Standard 5).

Ausgabe je Titel: Titel | WKN | Kurs (€) | Intraday | 1 Woche | Marktkap. (Mrd. €)

Läuft lokal (Ausgabe im Terminal) oder automatisch per GitHub Actions:
  --html docs/index.html   schreibt eine Handy-taugliche Ergebnisseite
  --state docs/state.json  merkt sich die Treffer des letzten Laufs
  NTFY_TOPIC (Umgebung)    schickt eine Push-Nachricht, wenn NEUE Titel auftauchen

Installation:  pip install yfinance pandas requests lxml
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

BERLIN = ZoneInfo("Europe/Berlin")
HEADERS = {"User-Agent": "Mozilla/5.0 (aktien-crash-scanner)"}
CACHE_DIR = Path(os.environ.get("SCANNER_CACHE", Path.home() / ".aktien_crash_scanner"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Indizes: Wikipedia-Seite, Spaltenname mit dem Ticker, Yahoo-Börsenkürzel
# --------------------------------------------------------------------------
INDIZES = {
    "S&P 500":       dict(url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                          cols=["Symbol"], style="us"),
    "Nasdaq-100":    dict(url="https://api.nasdaq.com/api/quote/list-type/nasdaq100",
                          cols=[], style="us", nasdaq_api=True),
    "DAX":           dict(url="https://en.wikipedia.org/wiki/DAX",
                          cols=["Ticker", "Symbol"], suffix=".DE"),
    "MDAX":          dict(url="https://en.wikipedia.org/wiki/MDAX",
                          cols=["Symbol", "Ticker"], suffix=".DE"),
    "Euro Stoxx 50": dict(url="https://en.wikipedia.org/wiki/EURO_STOXX_50",
                          cols=["Ticker", "Symbol"], suffix=None),
    "FTSE 100":      dict(url="https://en.wikipedia.org/wiki/FTSE_100_Index",
                          cols=["Ticker", "EPIC"], style="lse"),
    "CAC 40":        dict(url="https://en.wikipedia.org/wiki/CAC_40",
                          cols=["Ticker", "Symbol"], suffix=".PA"),
    "SMI":           dict(url="https://en.wikipedia.org/wiki/Swiss_Market_Index",
                          cols=["Ticker", "Symbol"], suffix=".SW"),
    "AEX":           dict(url="https://en.wikipedia.org/wiki/AEX_index",
                          cols=["Ticker symbol", "Ticker", "Symbol"], suffix=".AS"),
    "IBEX 35":       dict(url="https://en.wikipedia.org/wiki/IBEX_35",
                          cols=["Ticker", "Symbol"], suffix=".MC"),
    "FTSE MIB":      dict(url="https://en.wikipedia.org/wiki/FTSE_MIB",
                          cols=["Ticker", "Symbol"], suffix=".MI"),
    "Hang Seng":     dict(url="https://en.wikipedia.org/wiki/Hang_Seng_Index",
                          cols=["Ticker", "Stock code", "Code"], style="hk",
                          regex=r"SEHK\s*:\s*(\d{1,5})"),
    "Nikkei 225":    dict(url="https://en.wikipedia.org/wiki/Nikkei_225",
                          cols=["Code", "Ticker", "Symbol"], style="jp",
                          regex=r"(?:TYO|TSE)\s*:\s*([0-9][0-9A-Z]{3})\b"),
}

# Währungen, in denen Yahoo Kurse in Hundertsteln angibt
SUB_UNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}


# --------------------------------------------------------------------------
# Indexmitglieder laden
# --------------------------------------------------------------------------
def normalize(raw, cfg):
    """Wikipedia-Tickertext -> Yahoo-Ticker."""
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None
    s = s.split(":")[-1].strip()
    if not s:
        return None
    s = s.split()[0]
    style, suffix = cfg.get("style"), cfg.get("suffix")

    if style == "us":
        return s.replace(".", "-").upper()
    if style == "hk":
        d = re.sub(r"\D", "", s)
        return f"{int(d):04d}.HK" if d else None
    if style == "jp":
        d = s.upper().split(".")[0]
        return f"{d}.T" if re.fullmatch(r"[0-9][0-9A-Z]{3}", d) else None
    if style == "lse":
        if s.upper().endswith(".L"):
            s = s[:-2]
        s = s.rstrip(".").replace(".", "-").upper()
        return s + ".L" if s else None

    if re.search(r"\.[A-Z]{1,3}$", s):
        return s
    return s + suffix if suffix else None


def fetch_index(cfg):
    if cfg.get("nasdaq_api"):
        r = requests.get(cfg["url"], timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"})
        rows = ((r.json().get("data") or {}).get("data") or {}).get("rows") or []
        return {t for t in (normalize(x.get("symbol"), cfg) for x in rows) if t}
    page = requests.get(cfg["url"], headers=HEADERS, timeout=30).text
    best = set()
    try:
        tables = pd.read_html(StringIO(page))
    except ValueError:
        tables = []
    for tb in tables:
        cols = {str(c[-1] if isinstance(c, tuple) else c).strip().lower(): c for c in tb.columns}
        for want in cfg["cols"]:
            col = cols.get(want.lower())
            if col is not None:
                found = {t for t in (normalize(v, cfg) for v in tb[col]) if t}
                if len(found) > len(best):
                    best = found
                break
    if len(best) < 5 and cfg.get("regex"):
        text = re.sub(r"<[^>]+>", "", page).replace("&#160;", " ").replace("&nbsp;", " ")
        best |= {t for t in (normalize(m, cfg) for m in re.findall(cfg["regex"], text)) if t}
    return best


def load_constituents(index_names):
    cache = CACHE_DIR / "indizes.json"
    stored = json.loads(cache.read_text()) if cache.exists() else {}
    data = stored.get("data", {}) if stored.get("datum") == str(date.today()) else {}
    for name in index_names:
        if data.get(name):
            continue
        try:
            data[name] = sorted(fetch_index(INDIZES[name]))
            print(f"  {name:<14} {len(data[name]):>4} Titel")
        except Exception as e:
            print(f"  {name:<14} FEHLER: {e}")
            data[name] = []
    cache.write_text(json.dumps({"datum": str(date.today()), "data": data}))
    mapping = {}
    for name in index_names:
        for t in data.get(name, []):
            mapping.setdefault(t, name)
    return mapping


# --------------------------------------------------------------------------
# Kurse & Analyse
# --------------------------------------------------------------------------
def download_closes(tickers, chunk=150):
    frames = []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        print(f"  Kurse {i + 1}–{i + len(part)} von {len(tickers)} …")
        df = yf.download(part, period="1mo", interval="1d", auto_adjust=True,
                         progress=False, threads=True)
        if df.empty:
            continue
        close = df["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(part[0])
        frames.append(close)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    return out


def analyse(closes, tage, schwelle):
    grenze = datetime.now() - timedelta(days=7)
    rows = []
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) < 7 or s.index[-1] < grenze:
            continue
        last = s.iloc[-1]
        ref = s.iloc[-(tage + 1):-1].max()       # höchster Schluss der letzten 1–N Tage
        fall = last / ref - 1
        if fall <= -schwelle:
            rows.append(dict(
                ticker=t,
                fall=fall,
                intraday=last / s.iloc[-2] - 1,  # heute vs. Vortagesschluss
                woche=last / s.iloc[-6] - 1,     # vs. Schluss vor 5 Handelstagen
                kurs=last,
            ))
    return pd.DataFrame(rows)


def fx_to_eur(currencies):
    """Kurs: Einheiten Fremdwährung je 1 EUR."""
    rates = {"EUR": 1.0}
    needed = [c for c in currencies if c and c != "EUR"]
    if needed:
        df = yf.download([f"EUR{c}=X" for c in needed], period="5d", progress=False)["Close"]
        if isinstance(df, pd.Series):
            df = df.to_frame(f"EUR{needed[0]}=X")
        last = df.ffill().iloc[-1]
        for c in needed:
            v = last.get(f"EUR{c}=X")
            if pd.notna(v):
                rates[c] = float(v)
    return rates


# --------------------------------------------------------------------------
# Stammdaten: Name, Marktkap., ISIN, WKN
# --------------------------------------------------------------------------
WKN_CACHE = CACHE_DIR / "wkn.json"


LEGAL = r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|ag|se|sa|s\.?a|s\.?p\.?a|n\.?v|nv|holdings?|group|kgaa|the)\b\.?"


def short_name(name):
    n = re.sub(r"[,()]", " ", name)
    n = re.sub(LEGAL, " ", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" .-&")


def onvista_search(q):
    import time
    for versuch in range(3):
        try:
            r = requests.get("https://api.onvista.de/api/v1/instruments/search/facet",
                             params={"searchValue": q}, headers=HEADERS, timeout=15)
            if r.status_code == 429:
                time.sleep(3 * (versuch + 1))
                continue
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            data = r.json()
            facets = data.get("facets") if isinstance(data, dict) else None
            res = []
            for f in facets or []:
                if isinstance(f, dict):
                    res += [x for x in (f.get("results") or []) if isinstance(x, dict)]
            return res, f"{len(res)} Ergebnisse"
        except Exception as e:
            return None, type(e).__name__
    return None, "HTTP 429"


def lookup_wkn(isin, name, ticker, cache):
    isin = isin if isinstance(isin, str) and isin.strip() else None
    name = name if isinstance(name, str) and name.strip() else None
    key = isin or name or ticker
    if key in cache:
        return cache[key]
    if isin and isin.startswith("DE000") and len(isin) == 12:
        cache[key] = isin[5:11]                  # deutsche ISIN enthält die WKN
        return cache[key]

    queries = [q for q in (isin, short_name(name) if name else None, name,
                           ticker.split(".")[0] if ticker else None) if q]
    protokoll = []
    for q in dict.fromkeys(queries):
        res, info = onvista_search(q)
        protokoll.append(f"{q!r}: {info}")
        for x in res or []:
            if not x.get("wkn"):
                continue
            if x.get("entityType") not in (None, "STOCK"):
                continue
            if q == isin and x.get("isin") not in (None, isin):
                continue
            if q != isin and name:                  # Namenssuche: erstes Wort muss passen
                erstes = short_name(name).split(" ")[0].lower()
                if erstes and erstes not in str(x.get("name", "")).lower():
                    continue
            cache[key] = x["wkn"]
            return x["wkn"]
    print(f"  WKN nicht gefunden für {name or ticker} – " + "; ".join(protokoll))
    return "–"                                       # Fehlschläge nicht cachen


def enrich(hits):
    infos = []
    for t in hits["ticker"]:
        tk = yf.Ticker(t)
        name, cap, cur, isin = t, None, None, None
        try:
            fi = tk.fast_info
            cap, cur = fi.market_cap, fi.currency
        except Exception:
            pass
        try:
            info = tk.info
            name = info.get("longName") or info.get("shortName") or t
            if not cap:
                cap, cur = info.get("marketCap"), info.get("currency")
        except Exception:
            pass
        try:
            isin = tk.isin if tk.isin and tk.isin != "-" else None
        except Exception:
            pass
        teiler = 1
        if cur in SUB_UNITS:                     # z. B. London: Pence -> Pfund
            cur, teiler = SUB_UNITS[cur]
            if cap:
                cap = cap / teiler
        infos.append(dict(ticker=t, titel=name, kap=cap, waehrung=cur, teiler=teiler, isin=isin))
        print(f"  {t:<10} {name}")

    df = hits.merge(pd.DataFrame(infos), on="ticker")
    rates = fx_to_eur(df["waehrung"].dropna().unique().tolist())
    rate = df["waehrung"].map(rates)
    df["kap_eur_mrd"] = df["kap"] / rate / 1e9
    df["kurs_eur"] = df["kurs"] / df["teiler"] / rate
    return df


# --------------------------------------------------------------------------
# Formatierung
# --------------------------------------------------------------------------
def de(x, nachkomma=1):
    if x is None or pd.isna(x):
        return "–"
    return f"{x:,.{nachkomma}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x):
    return "–" if x is None or pd.isna(x) else f"{x * 100:+.1f} %".replace(".", ",")


def write_html(path, df, neu, a, stand):
    def color(x):
        return "neg" if x < 0 else "pos"

    if df.empty:
        body = f'<p class="leer">Aktuell kein Titel mit ≥ {a.schwelle:g} % Verlust in {a.tage} Tagen und &gt; {a.min_kap:g} Mrd. € Marktkap.</p>'
    else:
        cards = []
        for _, r in df.iterrows():
            badge = '<span class="neu">NEU</span>' if r["ticker"] in neu else ""
            such = r["isin"] if isinstance(r["isin"], str) else (r["wkn"] if r["wkn"] != "–" else r["titel"])
            link = f'https://www.onvista.de/suche/?searchValue={html.escape(str(such))}'
            cards.append(f"""
<a class="card" href="{link}">
  <div class="kopf"><span class="titel">{html.escape(str(r['titel']))}</span>{badge}</div>
  <div class="sub">WKN {html.escape(str(r['wkn']))} · {html.escape(str(r['index']))}</div>
  <div class="werte">
    <div><span class="lbl">Kurs</span><span class="val">{de(r['kurs_eur'], 2)} €</span></div>
    <div><span class="lbl">Intraday</span><span class="val {color(r['intraday'])}">{pct(r['intraday'])}</span></div>
    <div><span class="lbl">1 Woche</span><span class="val {color(r['woche'])}">{pct(r['woche'])}</span></div>
    <div><span class="lbl">Marktkap.</span><span class="val">{de(r['kap_eur_mrd'])} Mrd €</span></div>
  </div>
</a>""")
        body = "\n".join(cards)

    page = f"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kursrutsch-Scanner</title>
<style>
:root {{ --bg:#f6f6f4; --card:#fff; --text:#1b1b1b; --mut:#6b6b6b; --line:#e4e4e0; --neg:#c62828; --pos:#2e7d32; --acc:#1f5fbf; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --card:#1f1f1f; --text:#eee; --mut:#9a9a9a; --line:#2e2e2e; --neg:#ef6b6b; --pos:#6fcf73; --acc:#7aa7ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.4 -apple-system,system-ui,Segoe UI,Roboto,sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:16px; }}
h1 {{ font-size:22px; margin:4px 0 2px; }}
.meta {{ color:var(--mut); font-size:13px; margin-bottom:16px; }}
.card {{ display:block; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-bottom:10px; color:inherit; text-decoration:none; }}
.kopf {{ display:flex; justify-content:space-between; gap:8px; align-items:baseline; }}
.titel {{ font-weight:600; font-size:16px; }}
.neu {{ background:var(--acc); color:#fff; font-size:11px; font-weight:700; padding:2px 7px; border-radius:99px; }}
.sub {{ color:var(--mut); font-size:12.5px; margin:2px 0 10px; }}
.werte {{ display:grid; grid-template-columns:repeat(4,1fr); gap:6px; }}
.werte div {{ display:flex; flex-direction:column; }}
.lbl {{ color:var(--mut); font-size:11.5px; }}
.val {{ font-variant-numeric:tabular-nums; font-weight:600; font-size:14px; }}
.neg {{ color:var(--neg); }} .pos {{ color:var(--pos); }}
.leer {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; color:var(--mut); }}
footer {{ color:var(--mut); font-size:12px; margin-top:18px; }}
@media (max-width:420px) {{ .werte {{ grid-template-columns:repeat(2,1fr); row-gap:10px; }} }}
</style></head>
<body><main>
<h1>Kursrutsch-Scanner</h1>
<div class="meta">≥ {a.schwelle:g} % Verlust in {a.tage} Handelstagen · Marktkap. &gt; {a.min_kap:g} Mrd. € · Stand {stand:%d.%m.%Y, %H:%M} Uhr · {len(df)} Treffer</div>
{body}
<footer>Daten: Yahoo Finance (teils verzögert), Indexlisten: Wikipedia. Keine Anlageberatung.</footer>
</main></body></html>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(page, encoding="utf-8")


def notify(topic, neu_df, page_url):
    zeilen = [f"{r['titel'][:28]}: {pct(r['fall'])} ({de(r['kurs_eur'], 2)} EUR)" for _, r in neu_df.iterrows()]
    msg = "\n".join(zeilen[:10]) + (f"\n… und {len(zeilen) - 10} weitere" if len(zeilen) > 10 else "")
    headers = {"Title": f"Kursrutsch-Scanner: {len(zeilen)} neue Titel",
               "Tags": "chart_with_downwards_trend", "Priority": "high"}
    if page_url:
        headers["Click"] = page_url
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), headers=headers, timeout=15)
        print("Push-Nachricht gesendet.")
    except Exception as e:
        print(f"Push fehlgeschlagen: {e}")


# --------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------
def scan(a):
    print("1) Indexmitglieder laden …")
    mapping = load_constituents(a.indizes)
    tickers = sorted(mapping)
    if not tickers:
        sys.exit("Keine Indexmitglieder gefunden.")
    print(f"   {len(tickers)} Titel insgesamt\n")

    print("2) Kurse abrufen …")
    hits = analyse(download_closes(tickers), a.tage, a.schwelle / 100)
    print(f"   {len(hits)} Titel mit ≥{a.schwelle:g} % Verlust\n")
    if hits.empty:
        return pd.DataFrame()

    print("3) Stammdaten & Marktkapitalisierung …")
    df = enrich(hits)
    df = df[df["kap_eur_mrd"].fillna(0) >= a.min_kap].copy()
    if df.empty:
        return df

    print("\n4) WKN ermitteln …")
    wkn_cache = json.loads(WKN_CACHE.read_text()) if WKN_CACHE.exists() else {}
    df["wkn"] = [lookup_wkn(i, n, t, wkn_cache) for i, n, t in zip(df["isin"], df["titel"], df["ticker"])]
    WKN_CACHE.write_text(json.dumps(wkn_cache, ensure_ascii=False, indent=0))
    df["index"] = df["ticker"].map(mapping)
    df = df.sort_values("fall")
    # Doppelt gelistete Aktien (z. B. Mailand + Paris) nur einmal zeigen
    df["_key"] = df["isin"].where(df["isin"].notna(), df["titel"])
    return df.drop_duplicates("_key").drop(columns="_key")


def main():
    ap = argparse.ArgumentParser(description="Sucht Aktien mit starkem Kursrutsch in großen Indizes.")
    ap.add_argument("--tage", type=int, default=3, help="Zeitfenster in Handelstagen (Standard 3)")
    ap.add_argument("--schwelle", type=float, default=10, help="Mindestverlust in %% (Standard 10)")
    ap.add_argument("--min-kap", type=float, default=5, help="Mindest-Marktkap. in Mrd. EUR (Standard 5)")
    ap.add_argument("--indizes", nargs="+", default=list(INDIZES), choices=list(INDIZES))
    ap.add_argument("--html", help="Ergebnisseite (HTML) hierhin schreiben")
    ap.add_argument("--state", help="Datei, in der die Treffer des letzten Laufs gespeichert werden")
    ap.add_argument("--einmal-taeglich-ab", type=int, metavar="STUNDE",
                    help="Nur laufen, wenn es in Berlin mind. STUNDE Uhr ist und heute noch kein Lauf war")
    a = ap.parse_args()

    jetzt = datetime.now(BERLIN)
    state_path = Path(a.state) if a.state else None
    state = json.loads(state_path.read_text()) if state_path and state_path.exists() else {}

    if a.einmal_taeglich_ab is not None:
        if jetzt.hour < a.einmal_taeglich_ab or state.get("letzter_lauf") == str(jetzt.date()):
            print(f"Übersprungen ({jetzt:%H:%M} Uhr Berlin, letzter Lauf {state.get('letzter_lauf')}).")
            return

    df = scan(a)
    vorher = set(state.get("ticker", []))
    jetzt_set = set(df["ticker"]) if not df.empty else set()
    neu = jetzt_set - vorher

    if df.empty:
        print(f"\nKeine Treffer (≥{a.schwelle:g} %, > {a.min_kap:g} Mrd. €).")
    else:
        out = pd.DataFrame({
            "Titel": df["titel"].str.slice(0, 32),
            "WKN": df["wkn"],
            "Kurs (€)": df["kurs_eur"].map(lambda x: de(x, 2)),
            "Intraday": df["intraday"].map(pct),
            "1 Woche": df["woche"].map(pct),
            "Marktkap. (Mrd. €)": df["kap_eur_mrd"].map(de),
        })
        print(f"\n=== Kursrutsch ≥{a.schwelle:g} % in {a.tage} Tagen, Marktkap. > {a.min_kap:g} Mrd. € "
              f"(Stand {jetzt:%d.%m.%Y %H:%M}) ===\n")
        print(out.to_string(index=False))
        print(f"\nNeu seit letztem Lauf: {len(neu)}")

    if a.html:
        write_html(a.html, df, neu, a, jetzt)
        print(f"Seite geschrieben: {a.html}")

    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("Hinweis: NTFY_TOPIC ist nicht gesetzt – keine Push-Nachricht.")
    elif neu:
        notify(topic, df[df["ticker"].isin(neu)], os.environ.get("PAGE_URL"))

    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # Nur geplante Läufe zählen als "heutiger Lauf"; manuelle Starts blockieren den 17-Uhr-Lauf nicht
        letzter = str(jetzt.date()) if a.einmal_taeglich_ab is not None else state.get("letzter_lauf")
        state_path.write_text(json.dumps({"letzter_lauf": letzter,
                                          "ticker": sorted(jetzt_set)}, indent=1))


if __name__ == "__main__":
    main()
