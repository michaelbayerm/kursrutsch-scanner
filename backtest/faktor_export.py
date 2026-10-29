"""
Lädt Kursverläufe (inkl. Ausschüttungen) von Welt- und Faktor-ETFs und speichert
Tagesschlusskurse nach backtest/daten/faktor_etfs.csv.gz. Ausgewertet wird lokal.
"""
import json
from pathlib import Path

import pandas as pd
import yfinance as yf

OUT = Path(__file__).resolve().parent / "daten"
OUT.mkdir(parents=True, exist_ok=True)

KANDIDATEN = {
    # Welt
    "EUNL.DE": "iShares Core MSCI World",
    "VGWL.DE": "Vanguard FTSE All-World (aussch.)",
    "VWCE.DE": "Vanguard FTSE All-World (thes.)",
    "SPPW.DE": "SPDR MSCI World",
    # Faktoren iShares (MSCI World ... Factor)
    "IS3R.DE": "iShares MSCI World Momentum",
    "IS3Q.DE": "iShares MSCI World Quality",
    "IS3S.DE": "iShares MSCI World Value",
    "IQQ0.DE": "iShares MSCI World Min Volatility",
    "IUSN.DE": "iShares MSCI World Small Cap",
    # Faktoren Xtrackers
    "XDEM.DE": "Xtrackers MSCI World Momentum",
    "XDEQ.DE": "Xtrackers MSCI World Quality",
    "XDEV.DE": "Xtrackers MSCI World Value",
    "XDEB.DE": "Xtrackers MSCI World Min Volatility",
    # Schwellenländer
    "IS3N.DE": "iShares Core MSCI EM IMI",
    # US-Varianten (längere Historie, in USD)
    "MTUM": "iShares MSCI USA Momentum (USD)",
    "QUAL": "iShares MSCI USA Quality (USD)",
    "VLUE": "iShares MSCI USA Value (USD)",
    "USMV": "iShares MSCI USA Min Vol (USD)",
    "URTH": "iShares MSCI World (USD)",
    "EURUSD=X": "EUR/USD",
}

df = yf.download(list(KANDIDATEN), start="2010-01-01", end="2026-10-29", interval="1d",
                 auto_adjust=True, progress=False, threads=True,
                 group_by="column", multi_level_index=True)["Close"]
if getattr(df.index, "tz", None) is not None:
    df.index = df.index.tz_localize(None)
df = df.sort_index()
df.to_csv(OUT / "faktor_etfs.csv.gz", float_format="%.6g", compression="gzip")
info = {t: dict(name=KANDIDATEN[t], start=str(df[t].first_valid_index())[:10] if t in df and df[t].notna().any() else None,
                ende=str(df[t].last_valid_index())[:10] if t in df and df[t].notna().any() else None)
        for t in KANDIDATEN}
(OUT / "faktor_etfs.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
print(json.dumps(info, indent=1, ensure_ascii=False))
