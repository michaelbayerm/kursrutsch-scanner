# Kursrutsch-Scanner

Prüft täglich um 17 Uhr (deutsche Zeit) die großen Indizes weltweit auf Aktien,
die in 1–3 Handelstagen um mehr als 10 % gefallen sind (Marktkap. > 5 Mrd. €).

- **Ergebnisse am Handy:** `https://<dein-github-name>.github.io/<repo-name>/`
- **Push-Nachricht** über die App *ntfy*, sobald NEUE Titel auf der Liste stehen
- Läuft kostenlos auf GitHub – dein PC muss nicht an sein

## Einrichtung (einmalig, ca. 10 Minuten)

1. **Repository anlegen:** Auf github.com → „New repository“, Name z. B.
   `kursrutsch-scanner`, *Public* (GitHub Pages ist für private Repos kostenpflichtig;
   sichtbar ist nur die Trefferliste).
2. **Dateien hochladen:** „Add file → Upload files“ und den gesamten Inhalt dieses
   Ordners hineinziehen (inkl. des Ordners `.github`).
3. **Push-Kanal hinterlegen:** Settings → Secrets and variables → Actions →
   „New repository secret“
   Name: `NTFY_TOPIC`  ·  Wert: `kursrutsch-36a44189f5`
4. **Webseite aktivieren:** Settings → Pages → Source „Deploy from a branch“,
   Branch `main`, Ordner `/docs` → Save.
5. **Handy:** App **ntfy** installieren (iOS/Android), „+“ → Thema
   `kursrutsch-36a44189f5` abonnieren.
6. **Testlauf:** Reiter „Actions“ → „Kursrutsch-Scanner“ → „Run workflow“.
   Nach ca. 5 Minuten steht die Seite, und bei Treffern kommt die erste Nachricht.

Danach läuft alles automatisch jeden Tag um 17 Uhr.

## Anpassen

In `.github/workflows/scanner.yml`, Zeile mit `python aktien_crash_scanner.py`,
können Optionen ergänzt werden, z. B. `--schwelle 8 --tage 2 --min-kap 10`.

Lokal am PC: `pip install -r requirements.txt` und `python aktien_crash_scanner.py`.

Daten: Yahoo Finance (teils verzögert), Indexlisten von Wikipedia, WKN über onvista.
Keine Anlageberatung.
