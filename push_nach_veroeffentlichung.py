#!/usr/bin/env python3
"""Wartet, bis GitHub Pages die neue Seite ausliefert (und bis zur Wunschzeit), dann Push senden."""
import json, os, re, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BERLIN = ZoneInfo("Europe/Berlin")
push_file, html_file = Path(sys.argv[1]), Path(sys.argv[2])
nicht_vor = sys.argv[3] if len(sys.argv) > 3 else ""       # z. B. "19:30"
if not push_file.exists():
    print("Keine Push-Nachricht vorbereitet.")
    sys.exit(0)
topic, page = os.environ["NTFY_TOPIC"], os.environ.get("PAGE_URL", "")
push = json.loads(push_file.read_text(encoding="utf-8"))
scan_id = re.search(r'name="scan-id" content="(\d+)"', html_file.read_text(encoding="utf-8")).group(1)

# 1) warten, bis die neue Seite online ist (max. 15 Min.)
online = False
for versuch in range(90):
    try:
        r = requests.get(page, params={"v": f"{scan_id}-{versuch}"}, timeout=15,
                         headers={"Cache-Control": "no-cache"})
        if scan_id in r.text:
            online = True
            break
    except Exception:
        pass
    time.sleep(10)
print("Seite online." if online else "Seite nach 15 Min. noch nicht aktualisiert – sende trotzdem.")

# 2) nicht vor der Wunschzeit senden
if nicht_vor:
    hh, mm = map(int, nicht_vor.split(":"))
    jetzt = datetime.now(BERLIN)
    ziel = jetzt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if jetzt < ziel:
        print(f"Warte bis {nicht_vor} Uhr …", flush=True)
        time.sleep((ziel - jetzt).total_seconds())

headers = push["headers"]
if page:
    headers["Click"] = f"{page}?v={scan_id}"              # frische Seite statt Zwischenspeicher
requests.post(f"https://ntfy.sh/{topic}", data=push["msg"].encode("utf-8"), headers=headers, timeout=15)
print(f"Push gesendet um {datetime.now(BERLIN):%H:%M:%S}.")
