#!/usr/bin/env python3
"""
Aktien-Crash-Scanner
====================
Durchsucht große Indizes weltweit nach Aktien, die in den letzten 1–2
Handelstagen stark gefallen sind (Standard: 15 % an deutscher Börse, 12 % an der Heimatbörse), mit einer
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
        k = kennzahlen(s, tage)
        if k["fall"] <= -schwelle:
            rows.append(dict(ticker=t, **k))
    return pd.DataFrame(rows)


def kennzahlen(s, tage):
    """Kennzahlen aus einer Reihe von Tagesschlusskursen (letzter Wert = aktueller Kurs)."""
    last = s.iloc[-1]
    return dict(fall=last / s.iloc[-(tage + 1):-1].max() - 1,
                intraday=last / s.iloc[-2] - 1,
                woche=last / s.iloc[-6] - 1,
                kurs=last, datum=s.index[-1])


# --------------------------------------------------------------------------
# Deutsche Börsenplätze: Stuttgart, sonst Frankfurt, sonst München
# --------------------------------------------------------------------------
DE_BOERSEN = [(".SG", "Stuttgart"), (".F", "Frankfurt"), (".MU", "München")]
DE_SUFFIXE = (".SG", ".F", ".MU", ".DE", ".BE", ".DU", ".HM", ".HA")
BOERSEN_CACHE = CACHE_DIR / "boersen.json"


def deutsches_kuerzel(ticker, isin, name, cache):
    """Yahoo-Grundsymbol der deutschen Notierung (z. B. 'MTE' für Micron) oder ''."""
    if ticker in cache:
        return cache[ticker]
    if ticker.endswith(".DE"):
        cache[ticker] = ticker[:-3]
        return cache[ticker]
    erstes = (short_name(name).split(" ")[0].lower() if isinstance(name, str) else "")
    basis = ""
    for q in (isin, short_name(name) if isinstance(name, str) else None, ticker.split(".")[0]):
        if not q or basis:
            continue
        try:
            quotes = yf.Search(q, max_results=25, news_count=0).quotes
        except Exception as e:
            print(f"  Börsensuche {q!r}: {type(e).__name__}")
            continue
        for x in quotes:
            sym = str(x.get("symbol", ""))
            if x.get("quoteType") not in (None, "EQUITY") or not sym.endswith(DE_SUFFIXE):
                continue
            if q != isin:                               # Namenssuche: Name muss passen
                n = f"{x.get('longname') or ''} {x.get('shortname') or ''}".lower()
                if erstes and erstes not in n:
                    continue
            basis = sym.rsplit(".", 1)[0]
            break
    if basis:                                       # Fehlschläge nicht dauerhaft merken
        cache[ticker] = basis
    return basis


def deutsche_kurse(df, tage):
    """Ersetzt Kurs und Veränderungen durch Werte einer deutschen Börse (in EUR)."""
    cache = json.loads(BOERSEN_CACHE.read_text()) if BOERSEN_CACHE.exists() else {}
    basen = {t: deutsches_kuerzel(t, ISIN_FUND.get(t) or (i if isinstance(i, str) else None), n, cache)
             for t, i, n in zip(df["ticker"], df["isin"], df["titel"])}
    BOERSEN_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0))

    kandidaten = [b + suf for b in set(basen.values()) if b for suf, _ in DE_BOERSEN]
    closes = download_closes(kandidaten) if kandidaten else pd.DataFrame()
    grenze = datetime.now() - timedelta(days=4)
    heim_eur = dict(zip(df["ticker"], df["kurs_eur"]))
    heim_datum = dict(zip(df["ticker"], df["datum"]))
    gewaehlt, verworfen, gruende = {}, [], {}
    for t, b in basen.items():
        optionen, grund = [], []
        gruende[t] = grund
        if not b:
            grund.append("keine Notierung an Stuttgart, Frankfurt oder München gefunden")
        for rang, (suf, ort) in enumerate(DE_BOERSEN):
            sym = b + suf if b else None
            if not sym:
                continue
            if sym not in closes or len(closes[sym].dropna()) < 7:
                grund.append(f"{ort}: keine Kursdaten")
                continue
            ser = closes[sym].dropna()
            if ser.index[-1] < grenze:
                grund.append(f"{ort}: zuletzt gehandelt am {ser.index[-1]:%d.%m.}")
                continue
            ref = heim_eur.get(t)
            if ref and not pd.isna(ref) and not (0.8 <= ser.iloc[-1] / ref <= 1.25):
                verworfen.append(f"{sym} ({de(ser.iloc[-1], 2)} € statt ~{de(ref, 2)} €)")
                grund.append(f"{ort}: Kurs passt nicht zur Aktie ({de(ser.iloc[-1], 2)} € statt ~{de(ref, 2)} €)")
                continue                            # anderes Papier (z. B. ADR) oder Fehlkurs
            optionen.append((ser.index[-1], -rang, sym, ort, ser))
        # aktuellster Kurs zuerst, bei Gleichstand Stuttgart > Frankfurt > München;
        # ein Kurs, dessen letzter Handel älter ist als der letzte Handelstag der Heimatbörse, gilt als veraltet
        for _, _, sym, ort, ser in sorted(optionen, key=lambda o: (o[0], o[1]), reverse=True):
            zeit = None
            try:
                ts = yf.Ticker(sym).info.get("regularMarketTime")
                zeit = datetime.fromtimestamp(int(ts), BERLIN) if ts else None
            except Exception:
                pass
            hd = heim_datum.get(t)
            if zeit and hd is not None and not pd.isna(hd) and zeit.date() < pd.Timestamp(hd).date():
                verworfen.append(f"{sym} (letzter Handel {zeit:%d.%m. %H:%M}, veraltet)")
                grund.append(f"{ort}: letzter Handel {zeit:%d.%m. %H:%M} Uhr, veraltet")
                continue
            gewaehlt[t] = (sym, ort, ser, zeit)
            break
    if verworfen:
        print("   Verworfen: " + ", ".join(verworfen))

    zeilen = []
    for _, r in df.iterrows():
        r = r.copy()
        if r["ticker"] in gewaehlt:
            sym, ort, ser, zeit = gewaehlt[r["ticker"]]
            k = kennzahlen(ser, tage)
            for key, val in k.items():
                r[key] = val
            r["kurs_eur"] = k["kurs"]
            r["kurs_symbol"], r["boerse"], r["boerse_typ"] = sym, ort, "de"
            r["kurszeit"] = zeit
            sg = [g.split(": ", 1)[1] for g in gruende.get(r["ticker"], []) if g.startswith("Stuttgart:")]
            r["boerse_hinweis"] = ("; ".join(sg) or "keine Kursdaten") if ort != "Stuttgart" else ""
        else:
            r["kurs_symbol"] = r["ticker"]
            heim = r.get("heim") if isinstance(r.get("heim"), str) and r.get("heim") else "Heimatbörse"
            r["boerse"], r["boerse_typ"] = heim, "heim"
            r["boerse_hinweis"] = "; ".join(gruende.get(r["ticker"], [])) or "keine deutsche Notierung gefunden"
        zeilen.append(r)
    out = pd.DataFrame(zeilen)
    print(f"   {len(gewaehlt)} von {len(df)} Titeln mit deutschem Börsenkurs "
          f"({', '.join(f'{o}: {sum(1 for v in gewaehlt.values() if v[1] == o)}' for _, o in DE_BOERSEN)})")
    return out


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


ISIN_FUND = {}


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
            if x.get("isin"):
                ISIN_FUND[ticker] = x["isin"]
            return x["wkn"]
    print(f"  WKN nicht gefunden für {name or ticker} – " + "; ".join(protokoll))
    return "–"                                       # Fehlschläge nicht cachen


SEKTOREN = {
    "Technology": "Technologie", "Healthcare": "Gesundheit", "Financial Services": "Finanzen",
    "Consumer Cyclical": "Zyklischer Konsum", "Consumer Defensive": "Basiskonsum",
    "Industrials": "Industrie", "Energy": "Energie", "Utilities": "Versorger",
    "Basic Materials": "Grundstoffe", "Real Estate": "Immobilien",
    "Communication Services": "Kommunikation",
}
LAENDER = {
    "United States": "USA", "Germany": "Deutschland", "Japan": "Japan", "China": "China",
    "Hong Kong": "Hongkong", "United Kingdom": "Großbritannien", "France": "Frankreich",
    "Switzerland": "Schweiz", "Netherlands": "Niederlande", "Spain": "Spanien", "Italy": "Italien",
    "Ireland": "Irland", "Belgium": "Belgien", "Finland": "Finnland", "Luxembourg": "Luxemburg",
    "Canada": "Kanada", "Israel": "Israel", "Taiwan": "Taiwan", "South Korea": "Südkorea",
    "Singapore": "Singapur", "Australia": "Australien", "Bermuda": "Bermuda", "Cayman Islands":
    "Kaimaninseln", "Jersey": "Jersey", "Denmark": "Dänemark", "Sweden": "Schweden",
    "Norway": "Norwegen", "Austria": "Österreich", "Uruguay": "Uruguay", "Argentina": "Argentinien",
    "Brazil": "Brasilien", "Mexico": "Mexiko", "India": "Indien", "Chile": "Chile",
}

# --------------------------------------------------------------------------
# Nachrichten: Signalwörter der letzten Stunden
# --------------------------------------------------------------------------
NEWS_STUNDEN = 24
SIGNALE = {
    "Gewinnwarnung":         [r"gewinnwarnung", r"profit warning", r"\bwarns?\b", r"\bwarnt\b"],
    "Prognose gesenkt":      [r"prognose\w* (ge)?senk", r"senk\w* \w* ?(jahres|umsatz|gewinn)?(prognose|ausblick)", r"senk\w* \w* ?(jahres|umsatz|gewinn)ziel",
                              r"(cut|lower|slash|trim)s? \w* ?(guidance|outlook|forecast)",
                              r"(weak|soft|disappointing) (guidance|outlook|forecast)",
                              r"schwache[rn]? (ausblick|prognose)"],
    "Erwartungen verfehlt":  [r"enttäusch", r"disappoint", r"verfehlt", r"unter (den )?erwartungen",
                              r"miss(es|ed)? (estimates|expectations|forecasts)",
                              r"falls? short of", r"below (estimates|expectations)"],
    "Quartalszahlen":        [r"quartal", r"\bearnings\b", r"\bresults\b", r"\bq[1-4]\b", r"bilanz"],
    "Gewinnerwartung":       [r"gewinnerwartung", r"gewinnprognose", r"gewinnschätzung",
                              r"earnings (estimate|expectation|forecast)", r"\beps\b"],
    "Abstufung / Kursziel ↓": [r"downgrade", r"abgestuft", r"abstufung", r"herabgestuft",
                              r"kursziel\w* (ge)?senk", r"senk\w* \w* ?kursziel",
                              r"(cut|lower|slash)s? \w* ?(price )?target", r"price target cut"],
    "Gewinnmitnahmen":       [r"gewinnmitnahme", r"profit[- ]taking"],
    "Ausverkauf":            [r"sell-?off", r"ausverkauf", r"\brout\b", r"\bslump", r"\btumbl", r"\bplung",
                              r"\bsink", r"absturz", r"bricht ein", r"einbruch", r"kurssturz", r"\bcrash"],
    "KI-Blase":              [r"ai bubble", r"ki-blase", r"\bbubble\b", r"blase"],
    "Zölle / Handelsstreit": [r"tariff", r"\bzoll", r"\bzölle", r"trade war", r"handelsstreit",
                              r"export (control|curb|restriction|ban)", r"exportbeschränk",
                              r"sanktion", r"sanction"],
    "Klage / Ermittlungen":  [r"lawsuit", r"\bklage", r"investigation", r"ermittlung", r"\bprobe\b",
                              r"\bfraud", r"betrug", r"class action", r"sammelklage"],
    "Short-Seller":          [r"short[- ]sell", r"short report", r"leerverkäuf"],
    "Kapitalerhöhung":       [r"kapitalerhöhung", r"(stock|share) (offering|sale|placement)",
                              r"platzierung", r"dilut", r"verwässer"],
    "Management-Wechsel":    [r"ceo \w* ?(step|resign|exit|depart|out)", r"rücktritt", r"tritt \w* ?zurück",
                              r"\bresign"],
    "Übernahme / Fusion":    [r"übernahme", r"takeover", r"acquisition", r"\bmerger", r"fusion",
                              r"\bacquire"],
    "Schwache Nachfrage":    [r"weak(er|ening)? demand", r"schwache\w* nachfrage", r"nachfrageschwäche",
                              r"demand (slow|weak|cool)"],
    "Zinsen / Notenbank":    [r"\bfed\b", r"\bzins", r"rate (cut|hike)", r"interest rate", r"\bezb\b", r"\bboj\b"],
    "Rückruf / Störung":     [r"\brecall", r"rückruf", r"outage", r"störung"],
    "Dividende gekürzt":     [r"dividend (cut|suspen)", r"dividende\w* (gekürzt|gestrichen|ausgesetzt)",
                              r"kürz\w* \w* ?dividende"],
}
SIGNAL_RE = {k: re.compile("|".join(v), re.I) for k, v in SIGNALE.items()}


def _google_news(query, sprache):
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    lang = {"de": ("de", "DE", "DE:de"), "en": ("en-US", "US", "US:en")}[sprache]
    r = requests.get("https://news.google.com/rss/search",
                     params={"q": f"{query} when:1d", "hl": lang[0], "gl": lang[1], "ceid": lang[2]},
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    out = []
    for item in ET.fromstring(r.content).iter("item"):
        titel = item.findtext("title") or ""
        quelle = item.findtext("source") or ""
        if quelle and titel.endswith(" - " + quelle):
            titel = titel[: -len(quelle) - 3]
        try:
            zeit = parsedate_to_datetime(item.findtext("pubDate"))
        except Exception:
            zeit = None
        out.append(dict(titel=titel, text=titel, link=item.findtext("link"), quelle=quelle, zeit=zeit))
    return out


def _yahoo_news(tk):
    out = []
    for n in (tk.news or []):
        c = n.get("content", n) if isinstance(n, dict) else {}
        titel = c.get("title") or ""
        text = f"{titel} {c.get('summary') or c.get('description') or ''}"
        zeit = None
        if c.get("pubDate"):
            try:
                zeit = datetime.fromisoformat(str(c["pubDate"]).replace("Z", "+00:00"))
            except Exception:
                pass
        elif c.get("providerPublishTime"):
            zeit = datetime.fromtimestamp(int(c["providerPublishTime"]), BERLIN)
        link = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else c.get("link")
        quelle = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
        out.append(dict(titel=titel, text=text, link=link, quelle=quelle or "Yahoo Finance", zeit=zeit))
    return out


def wochen_kurven(tickers):
    """Kursverlauf der letzten 7 Tage im Stundentakt je Ticker."""
    try:
        df = yf.download(list(tickers), period="7d", interval="1h", auto_adjust=True,
                         progress=False, threads=True)
        close = df["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(list(tickers)[0])
    except Exception as e:
        print(f"  Wochenverläufe nicht verfügbar: {e}")
        return {}
    idx = pd.to_datetime(close.index)
    close.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    grenze = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
    out = {}
    for t in tickers:
        if t not in close:
            continue
        teil = close[t].dropna()
        teil = teil[teil.index >= grenze]
        if len(teil) >= 2:
            out[t] = dict(werte=[float(v) for v in teil.values],
                          von=teil.index[0].tz_convert(BERLIN), bis=teil.index[-1].tz_convert(BERLIN))
    return out


def sparkline(kurve, vorschluss, ort=""):
    """Kleines SVG-Diagramm; gestrichelte Linie = Schlusskurs vor einer Woche."""
    if not kurve:
        return ""
    w, h, pad = 300, 56, 4
    werte = kurve["werte"]
    basis = vorschluss if vorschluss and not pd.isna(vorschluss) else werte[0]
    lo, hi = min(werte + [basis]), max(werte + [basis])
    spanne = (hi - lo) or 1
    x = lambda i: pad + i * (w - 2 * pad) / max(len(werte) - 1, 1)
    y = lambda v: pad + (hi - v) * (h - 2 * pad) / spanne
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(werte))
    farbe = "var(--neg)" if werte[-1] < basis else "var(--pos)"
    flaeche = f"{x(0):.1f},{h - pad} {pts} {x(len(werte) - 1):.1f},{h - pad}"
    von, bis = kurve["von"], kurve["bis"]
    fmt = "%d.%m."
    return f"""
  <div class="chart">
    <div class="lbl">Wochenverlauf {ort} {von.strftime(fmt)} – {bis.strftime(fmt)} · gestrichelt = Kurs vor 1 Woche</div>
    <svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img" aria-label="Kursverlauf">
      <polygon points="{flaeche}" fill="{farbe}" opacity="0.12"/>
      <line x1="{pad}" x2="{w - pad}" y1="{y(basis):.1f}" y2="{y(basis):.1f}" stroke="var(--mut)" stroke-width="1" stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>
      <polyline points="{pts}" fill="none" stroke="{farbe}" stroke-width="1.8" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      <circle cx="{x(len(werte) - 1):.1f}" cy="{y(werte[-1]):.1f}" r="2.6" fill="{farbe}"/>
    </svg>
  </div>"""


def news_signale(titel, ticker):
    """Artikel der letzten Stunden laden und Signalwörter zählen (Anzahl Artikel je Signal)."""
    import time
    name = short_name(titel) or titel
    artikel, fehler = [], []
    for sprache, zusatz in (("de", "(Aktie OR Aktien)"), ("en", "(stock OR shares)")):
        try:
            artikel += _google_news(f'"{name}" {zusatz}', sprache)
        except Exception as e:
            fehler.append(f"Google-{sprache}: {type(e).__name__}")
        time.sleep(0.4)
    try:
        artikel += _yahoo_news(yf.Ticker(ticker))
    except Exception as e:
        fehler.append(f"Yahoo: {type(e).__name__}")

    grenze = datetime.now(BERLIN) - timedelta(hours=NEWS_STUNDEN)
    gesehen, frisch = set(), []
    for a in artikel:
        key = re.sub(r"\W+", "", a["titel"].lower())[:80]
        if not key or key in gesehen or (a["zeit"] and a["zeit"] < grenze):
            continue
        gesehen.add(key)
        frisch.append(a)

    zaehler = {}
    for a in frisch:
        for label, rx in SIGNAL_RE.items():
            if rx.search(a["text"]):
                zaehler[label] = zaehler.get(label, 0) + 1
    top = sorted(zaehler.items(), key=lambda kv: -kv[1])[:4]
    frisch.sort(key=lambda a: a["zeit"] or grenze, reverse=True)
    if fehler:
        print(f"  News-Fehler {name}: {', '.join(fehler)}")
    return dict(n=len(frisch), top=top, schlagzeile=frisch[0] if frisch else None)


def enrich(hits):
    infos = []
    for t in hits["ticker"]:
        tk = yf.Ticker(t)
        name, cap, cur, isin = t, None, None, None
        sektor, branche, land, kurszeit, heim = None, None, None, None, None
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
            sektor = SEKTOREN.get(info.get("sector"), info.get("sector"))
            branche = info.get("industry")
            land = LAENDER.get(info.get("country"), info.get("country"))
            heim = info.get("fullExchangeName") or info.get("exchange")
            ts = info.get("regularMarketTime")
            if ts:
                kurszeit = datetime.fromtimestamp(int(ts), BERLIN)
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
        infos.append(dict(ticker=t, titel=name, kap=cap, waehrung=cur, teiler=teiler, isin=isin,
                          sektor=sektor, branche=branche, land=land, kurszeit=kurszeit, heim=heim))
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


def kurszeit_text(r):
    kz = r.get("kurszeit")
    if isinstance(kz, datetime) and not pd.isna(kz):
        return kz.astimezone(BERLIN).strftime("%d.%m. %H:%M")
    d = r.get("datum")
    return pd.Timestamp(d).strftime("%d.%m.") if d is not None and not pd.isna(d) else "–"


def write_html(path, df, neu, a, stand, gesamt=None):
    def color(x):
        return "neg" if x < 0 else "pos"

    if df.empty:
        body = f'<p class="leer">Aktuell kein Titel mit ≥ {a.schwelle_de:g} % Verlust in {a.tage} Tagen und &gt; {a.min_kap:g} Mrd. € Marktkap.</p>'
    else:
        cards = []
        for _, r in df.iterrows():
            badge = '<span class="neu">NEU</span>' if r["ticker"] in neu else ""
            such = r["isin"] if isinstance(r["isin"], str) else (r["wkn"] if r["wkn"] != "–" else r["titel"])
            link = f'https://www.onvista.de/suche/?searchValue={html.escape(str(such))}'
            esc = lambda x: html.escape(str(x)) if isinstance(x, str) and x else ""
            herkunft = " · ".join(x for x in (esc(r.get("land")), esc(r.get("sektor"))) if x)
            branche = esc(r.get("branche"))

            ort = html.escape(str(r.get("boerse") or "–"))
            hinweis = html.escape(str(r.get("boerse_hinweis") or ""))
            if r.get("boerse_typ") == "de":
                quelle_html = (f'<div class="q-de"><b>Börse {ort}</b>Kurs, Veränderungen und Wochenverlauf von hier, in €'
                               + (f'<span class="grund">Stuttgart nicht verwendet: {hinweis}</span>' if hinweis else "") + '</div>')
            else:
                quelle_html = (f'<div class="q-heim"><b>⚠ Ausweichkurs: {ort} (Heimatbörse)</b>'
                               f'Kurs, Veränderungen und Wochenverlauf von dort, in € umgerechnet.'
                               f'<span class="grund">Grund: {hinweis}</span></div>')
            ort_kurz = ort if r.get("boerse_typ") == "de" else f"{ort} (umgerechnet)"

            nw = r.get("news") or {}
            if nw.get("n"):
                chips = "".join(f'<span class="chip">{html.escape(k)} <b>{v}</b></span>' for k, v in nw["top"])
                if not chips:
                    chips = '<span class="chip leise">keine Signalwörter</span>'
                sz = nw.get("schlagzeile") or {}
                headline = ""
                if sz.get("titel"):
                    h = html.escape(sz["titel"])
                    q = html.escape(sz.get("quelle") or "")
                    hl = html.escape(sz.get("link") or "")
                    headline = (f'<a class="headline" href="{hl}">{h}</a>' if hl else f'<div class="headline">{h}</div>') \
                        + (f'<span class="quelle">{q}</span>' if q else "")
                news_html = f"""
  <div class="news">
    <div class="lbl">Nachrichten {NEWS_STUNDEN} Std. · {nw['n']} Artikel</div>
    <div class="chips">{chips}</div>
    {headline}
  </div>"""
            else:
                news_html = f'<div class="news"><div class="lbl">Keine Nachrichten in den letzten {NEWS_STUNDEN} Std. gefunden</div></div>'

            cards.append(f"""
<div class="card">
  <div class="kopf"><a class="titel" href="{link}">{html.escape(str(r['titel']))}</a>{badge}</div>
  <div class="sub">WKN {html.escape(str(r['wkn']))} · {html.escape(str(r['index']))}{' · ' + herkunft if herkunft else ''}</div>
  {f'<div class="sub2">{branche}</div>' if branche else ''}
  {quelle_html}
  <div class="werte">
    <div><span class="lbl">Kurs {ort_kurz} · {kurszeit_text(r)}</span><span class="val">{de(r['kurs_eur'], 2)} €</span></div>
    <div><span class="lbl">Rutsch {a.tage} Tage</span><span class="val neg"><b>{pct(r['fall'])}</b></span></div>
    <div><span class="lbl">Intraday</span><span class="val {color(r['intraday'])}">{pct(r['intraday'])}</span></div>
    <div><span class="lbl">1 Woche</span><span class="val {color(r['woche'])}">{pct(r['woche'])}</span></div>
    <div><span class="lbl">Marktkap.</span><span class="val">{de(r['kap_eur_mrd'])} Mrd €</span></div>
  </div>{sparkline(r.get('kurve'), r['kurs'] / (1 + r['woche']) if r.get('woche') is not None else None, ort_kurz)}{news_html}
</div>""")
        body = "\n".join(cards)

    page = f"""<!doctype html>
<html lang="de"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kursrutsch-Scanner</title>
<style>
:root {{ --bg:#f6f6f4; --card:#fff; --text:#1b1b1b; --mut:#6b6b6b; --line:#e4e4e0; --neg:#c62828; --pos:#2e7d32; --acc:#1f5fbf; --chip:#fdecea; --chiptext:#8e1c1c; --de:#1f5fbf; --de-bg:#eaf1fc; --hm:#b45f00; --hm-bg:#fff3e0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141414; --card:#1f1f1f; --text:#eee; --mut:#9a9a9a; --line:#2e2e2e; --neg:#ef6b6b; --pos:#6fcf73; --acc:#7aa7ff; --chip:#3a2222; --chiptext:#f3b3b3; --de:#7aa7ff; --de-bg:#1b2638; --hm:#ffb04d; --hm-bg:#33260f; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.4 -apple-system,system-ui,Segoe UI,Roboto,sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:16px; }}
h1 {{ font-size:22px; margin:4px 0 2px; }}
.meta {{ color:var(--mut); font-size:13px; margin-bottom:16px; }}
.card {{ display:block; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-bottom:10px; color:inherit; text-decoration:none; }}
.kopf {{ display:flex; justify-content:space-between; gap:8px; align-items:baseline; }}
.titel {{ font-weight:600; font-size:16px; }}
.neu {{ background:var(--acc); color:#fff; font-size:11px; font-weight:700; padding:2px 7px; border-radius:99px; }}
.sub {{ color:var(--mut); font-size:12.5px; margin:2px 0 0; }}
.sub2 {{ color:var(--mut); font-size:12px; margin:1px 0 0; font-style:italic; }}
.q-de, .q-heim {{ margin-top:8px; font-size:12.5px; line-height:1.4; padding:6px 9px; border-radius:8px; border-left:4px solid; }}
.q-de {{ background:var(--de-bg); border-color:var(--de); color:var(--text); }}
.q-heim {{ background:var(--hm-bg); border-color:var(--hm); color:var(--text); }}
.q-de b, .q-heim b {{ display:block; }}
.q-de b {{ color:var(--de); }} .q-heim b {{ color:var(--hm); }}
.grund {{ display:block; color:var(--mut); font-size:11.5px; margin-top:2px; }}
.mini {{ display:inline-block; margin:0 4px 0 0; padding:0 7px; font-size:11.5px; font-weight:600; border-left-width:3px; }}
.legende {{ color:var(--mut); font-size:12px; margin:-6px 0 14px; line-height:1.9; }}
.werte {{ margin-top:10px; }}
.titel {{ color:inherit; text-decoration:none; }}
.news {{ border-top:1px solid var(--line); margin-top:10px; padding-top:8px; }}
.chart {{ margin-top:10px; }}
.chart svg {{ display:block; width:100%; height:56px; margin-top:3px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:5px; margin:5px 0 6px; }}
.chip {{ font-size:12px; padding:2px 8px; border-radius:99px; background:var(--chip); color:var(--chiptext); }}
.chip b {{ font-weight:700; }}
.chip.leise {{ background:transparent; border:1px dashed var(--line); color:var(--mut); }}
.headline {{ display:block; font-size:13px; color:var(--text); text-decoration:none; line-height:1.35; }}
.quelle {{ display:block; color:var(--mut); font-size:11.5px; margin-top:2px; }}
.werte {{ display:grid; grid-template-columns:repeat(5,1fr); gap:6px; }}
.werte div {{ display:flex; flex-direction:column; }}
.lbl {{ color:var(--mut); font-size:11.5px; }}
.val {{ font-variant-numeric:tabular-nums; font-weight:600; font-size:14px; }}
.neg {{ color:var(--neg); }} .pos {{ color:var(--pos); }}
.leer {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; color:var(--mut); }}
footer {{ color:var(--mut); font-size:12px; margin-top:18px; }}
@media (max-width:520px) {{ .werte {{ grid-template-columns:repeat(3,1fr); row-gap:10px; }} }}
</style></head>
<body><main>
<h1>Kursrutsch-Scanner</h1>
<div class="meta">Kriterium „Rutsch“: aktueller Kurs gegenüber dem höchsten Schlusskurs der letzten {a.tage} Handelstage – ≥ {a.schwelle_de:g} % an deutscher Börse (≥ {a.schwelle:g} % an der Heimatbörse, wenn es keinen deutschen Kurs gibt) · Marktkap. &gt; {a.min_kap:g} Mrd. € · Stand {stand:%d.%m.%Y, %H:%M} Uhr · {gesamt if gesamt is not None else len(df)} Treffer{f", die {len(df)} wichtigsten angezeigt (neue zuerst, dann nach Tagesminus)" if gesamt and gesamt > len(df) else " (neue zuerst, dann nach Tagesminus)"}</div>
<div class="legende"><span class="q-de mini">Deutsche Börse</span> Kurs, Veränderungen und Wochenverlauf von Stuttgart, Frankfurt oder München, in €.
<span class="q-heim mini">Ausweichkurs</span> kein brauchbarer deutscher Kurs – Werte von der Heimatbörse, in € umgerechnet.</div>
{body}
<footer>Kurse und Veränderungen: deutsche Börse (Stuttgart, ersatzweise Frankfurt oder München) über Yahoo Finance, teils verzögert; Uhrzeit = letzter Kurs in deutscher Zeit. Ohne deutsche Notierung: Heimatbörse, in Euro umgerechnet. Nachrichten: Google News &amp; Yahoo Finance, Zahl = Anzahl Artikel mit diesem Signalwort. Indexlisten: Wikipedia. Keine Anlageberatung.</footer>
</main></body></html>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(page, encoding="utf-8")


def notify(topic, neu_df, page_url):
    def signal(r):
        top = (r.get("news") or {}).get("top") or []
        return f" – {top[0][0]}" if top else ""
    zeilen = [f"{r['titel'][:28]}: {pct(r['fall'])} ({de(r['kurs_eur'], 2)} EUR){signal(r)}" for _, r in neu_df.iterrows()]
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
    # Vorauswahl an der Heimatbörse, Endauswahl mit der (strengeren) Schwelle an der deutschen Börse
    hits = analyse(download_closes(tickers), a.tage, a.schwelle / 100)
    print(f"   {len(hits)} Titel mit ≥{a.schwelle:g} % Verlust an der Heimatbörse\n")
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
    df["isin"] = [i if isinstance(i, str) else ISIN_FUND.get(t) for i, t in zip(df["isin"], df["ticker"])]
    # Doppelt gelistete Aktien (z. B. Mailand + Paris) nur einmal zeigen
    df = df.sort_values("fall")
    df["_key"] = df["isin"].where(df["isin"].notna(), df["titel"])
    df = df.drop_duplicates("_key").drop(columns="_key")

    print("\n5) Kurse an deutscher Börse (Stuttgart › Frankfurt › München) …")
    df = deutsche_kurse(df, a.tage)
    grenze = df["boerse_typ"].map({"de": a.schwelle_de, "heim": a.schwelle}) / 100
    df = df[df["fall"] <= -grenze]
    print(f"   {len(df)} Titel erfüllen die Schwelle (≥{a.schwelle_de:g} % an deutscher Börse, "
          f"≥{a.schwelle:g} % an der Heimatbörse, falls kein deutscher Kurs)")
    return df.sort_values("fall")


def details(df):
    """Nachrichten und Wochenverlauf – nur für die angezeigten Titel."""
    print(f"\n6) Nachrichten der letzten {NEWS_STUNDEN} Std. auswerten …")
    df["news"] = [news_signale(n, t) for n, t in zip(df["titel"], df["ticker"])]
    print("\n7) Wochenverläufe laden …")
    kurven = wochen_kurven(df["kurs_symbol"].tolist())
    df["kurve"] = df["kurs_symbol"].map(lambda t: kurven.get(t))
    print(f"   {len(kurven)} von {len(df)} Verläufen geladen")
    return df


def ostersonntag(jahr):
    a, b, c = jahr % 19, jahr // 100, jahr % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    monat = (h + l - 7 * m + 114) // 31
    tag = (h + l - 7 * m + 114) % 31 + 1
    return date(jahr, monat, tag)


def bundesweiter_feiertag(tag):
    o = ostersonntag(tag.year)
    feiertage = {
        date(tag.year, 1, 1): "Neujahr",
        o - timedelta(days=2): "Karfreitag",
        o + timedelta(days=1): "Ostermontag",
        date(tag.year, 5, 1): "Tag der Arbeit",
        o + timedelta(days=39): "Christi Himmelfahrt",
        o + timedelta(days=50): "Pfingstmontag",
        date(tag.year, 10, 3): "Tag der Deutschen Einheit",
        date(tag.year, 12, 25): "1. Weihnachtstag",
        date(tag.year, 12, 26): "2. Weihnachtstag",
    }
    return feiertage.get(tag)


def main():
    ap = argparse.ArgumentParser(description="Sucht Aktien mit starkem Kursrutsch in großen Indizes.")
    ap.add_argument("--tage", type=int, default=2, help="Zeitfenster in Handelstagen (Standard 2)")
    ap.add_argument("--schwelle", type=float, default=12, help="Mindestverlust an der Heimatbörse in %% (Standard 12)")
    ap.add_argument("--schwelle-de", type=float, default=15, help="Mindestverlust an deutscher Börse in %% (Standard 15)")
    ap.add_argument("--min-kap", type=float, default=5, help="Mindest-Marktkap. in Mrd. EUR (Standard 5)")
    ap.add_argument("--indizes", nargs="+", default=list(INDIZES), choices=list(INDIZES))
    ap.add_argument("--max", type=int, default=25, help="Höchstens so viele Titel anzeigen (Standard 25)")
    ap.add_argument("--html", help="Ergebnisseite (HTML) hierhin schreiben")
    ap.add_argument("--state", help="Datei, in der die Treffer des letzten Laufs gespeichert werden")
    ap.add_argument("--einmal-taeglich-ab", type=int, metavar="STUNDE",
                    help="Nur laufen, wenn es in Berlin mind. STUNDE Uhr ist und heute noch kein Lauf war")
    a = ap.parse_args()

    jetzt = datetime.now(BERLIN)
    state_path = Path(a.state) if a.state else None
    state = json.loads(state_path.read_text()) if state_path and state_path.exists() else {}

    if a.einmal_taeglich_ab is not None:
        if jetzt.weekday() >= 5:
            print(f"Übersprungen: Wochenende ({['Samstag', 'Sonntag'][jetzt.weekday() - 5]}, {jetzt:%d.%m.%Y}).")
            return
        if bundesweiter_feiertag(jetzt.date()):
            print(f"Übersprungen: Feiertag ({bundesweiter_feiertag(jetzt.date())}).")
            return
        if jetzt.hour < a.einmal_taeglich_ab or state.get("letzter_lauf") == str(jetzt.date()):
            print(f"Übersprungen ({jetzt:%H:%M} Uhr Berlin, letzter Lauf {state.get('letzter_lauf')}).")
            return

    df = scan(a)
    vorher = set(state.get("ticker", []))
    jetzt_set = set(df["ticker"]) if not df.empty else set()
    neu = jetzt_set - vorher
    if not df.empty:
        df["_neu"] = df["ticker"].isin(neu)
        df = df.sort_values(["_neu", "intraday"], ascending=[False, True]).drop(columns="_neu")
        gesamt = len(df)
        df = df.head(a.max).copy()
        details(df)
    else:
        gesamt = 0

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
            "Börse": df["boerse"].fillna("–").str.slice(0, 14),
            "Land": df["land"].fillna("–"),
            "Kurszeit": [kurszeit_text(r) for _, r in df.iterrows()],
            "News": [f"{(n or {}).get('n', 0)}: " + ", ".join(f"{k} {v}" for k, v in (n or {}).get("top", [])[:2]) for n in df["news"]],
        })
        print(f"\n=== Kursrutsch ≥{a.schwelle:g} % in {a.tage} Tagen, Marktkap. > {a.min_kap:g} Mrd. € "
              f"(Stand {jetzt:%d.%m.%Y %H:%M}) ===\n")
        print(out.to_string(index=False))
        print(f"\nNeu seit letztem Lauf: {len(neu)} · angezeigt {len(df)} von {gesamt}")

    if a.html:
        write_html(a.html, df, neu, a, jetzt, gesamt)
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
