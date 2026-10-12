# 3D Manager

Selbst gehostete Web-App für den 3D-Druck im Homelab: **Filament-, Material- und Zubehörbestand**, **Bambu Lab AMS live**, **RFID-Tags** (auch auf Fremdhersteller-Spulen) und **Druckerstatus** – in einem Docker-Container, optimiert für Desktop und Handy.

![Filament](docs/screenshots/filament.png)

## Funktionen

**Inventar**
- Filament mit **Farbmuster pro Spule** – offizielle Bambu-Farbwerte (über die 5-stellige Bambu-ID), Community-Farbwerte für Sunlu, Anycubic, Polymaker, eSun u. v. m., Verläufe, Transparenz, Matt/Silk/Galaxy-Optik, eigenes Foto vom gedruckten Muster
- Kacheln, **sortierbare Tabelle** und **Inventur-Modus** (große Zeilen, „heute geprüft“, Fortschritt)
- OVP / Spule / Refill / Muster, Lagerort, Mindestbestand („nachkaufen“), Restgewicht, Notizen
- Reiter **Material** (Holz, Acryl, Vinyl …) und **Zubehör** (Düsen, Druckplatten, Magnete …)
- Pro Eintrag **Hersteller**, **Hersteller-Link** und **Bezugsquelle (Shop-Link)** – 🛒 direkt in der Liste
- Export als JSON-Backup und CSV (Excel), Import des Backups

**Bambu Lab**
- **AMS live per MQTT** (LAN mit Zugangscode oder über die Bambu-Cloud): Slots, Restmenge, AMS-Feuchte/-Temperatur, AMS HT und externe Spule
- **RFID-Tags frei zuordnen**: Ein Tag ist nur eine ID – ein umgesetzter Bambu-Tag „PLA Basic Schwarz“ kann z. B. für eine Sunlu PLA+ Pink stehen. Restmenge und Verbrauch laufen dann auf diese Spule. Zuordnung per **Volltextsuche**
- **Bambu Filament Manager** (Cloud-Spulenbibliothek von Bambu Studio/Handy) wird gelesen und mit den AMS-Tags zusammengeführt
- **Druckerstatus**: Fortschritt, Schicht, Restzeit, Fertig-Uhrzeit, Temperaturen (Düse/Bett/Kammer), aktives Filament, Lüfter, HMS-Fehler im Klartext (deutsch)
- **Druckhistorie** mit Filamentverbrauch je Druck und Spule

**Bedienung**
- Dark Mode (Standard), Hell, System
- Handy: Navigation unten, Karten statt Tabellen, große Touch-Ziele, Dialoge als Sheet
- Installierbar als App (PWA) mit Kurzbefehlen

| Druckerstatus | AMS live | RFID-Tags | Handy |
|---|---|---|---|
| ![Drucker](docs/screenshots/drucker.png) | ![AMS](docs/screenshots/ams.png) | ![Tags](docs/screenshots/rfid-tags.png) | ![Mobil](docs/screenshots/mobil-inventur.png) |

## Installation

Voraussetzung: Docker mit Compose.

```bash
git clone https://github.com/Qu3ntyn83/3D-Manager.git
cd 3D-Manager
docker compose up -d --build
```

Danach erreichbar unter `http://<docker-host>:8095`.

- Daten liegen in `./data` (`inventar.db` + `uploads/` für Fotos) → ins Backup aufnehmen.
- `SEED=1` in der `docker-compose.yml` füllt eine leere Datenbank beim ersten Start mit einem Beispiel-Inventar.
- `BAMBU_MOCK=/app/mock_spools.json` zeigt Beispiel-Spulen der Cloud-Bibliothek ohne Bambu-Account.

**Update**

```bash
git pull
docker compose up -d --build
```

Neue Datenbankfelder werden beim Start automatisch angelegt; vorhandene Daten bleiben erhalten.

## Drucker verbinden

**Bambu & AMS → Drucker & Konto → + Drucker**

| Modus | Benötigt | Hinweis |
|---|---|---|
| **LAN** (empfohlen) | IP, Seriennummer, 8-stelliger LAN-Zugangscode (Druckerdisplay → Einstellungen → Netzwerk) | MQTT/TLS auf Port 8883, ohne Internet. Wird die Verbindung abgelehnt, am Drucker LAN-Modus bzw. Entwicklermodus prüfen. |
| **Cloud** | Anmeldung mit dem Bambu-Account (s. u.) | „Aus Bambu-Account übernehmen“ legt alle Drucker des Kontos an. |

Die App hält pro Drucker eine MQTT-Verbindung, fordert alle 5 Minuten einen Vollstatus an und verarbeitet die laufenden Delta-Meldungen.

## Bambu-Account / Filament Manager

**Bambu & AMS → Drucker & Konto**: E-Mail + Passwort (Bambu schickt meist einen Code per Mail), 2FA wird unterstützt. Konten, die über **Google/Apple** angelegt wurden, haben kein Passwort → **„Code per Mail“** verwenden. Alternativ ein vorhandenes Token einfügen.

- Gespeichert wird nur das Token (ca. 90 Tage gültig), nicht das Passwort.
- „Verbindung zur Bambu-Cloud testen“ prüft curl_cffi, DNS und HTTPS – bei `Could not resolve host` dem Container einen DNS-Server geben (siehe `dns:` in der `docker-compose.yml`) oder global in `/etc/docker/daemon.json`.
- Es wird **nur gelesen**, nichts in die Cloud geschrieben.

## RFID-Tags

Ein Tag ist nur eine ID. Was Bambu über den Tag meldet (Material/Farbe), wird nur als Info angezeigt.

- **RFID-Tags** listet alle Tags – aus dem AMS (MQTT) und aus der Bambu-Cloud – in einer Liste. Jeder Tag lässt sich per Suche **jeder** Inventar-Spule zuordnen; ebenso direkt im AMS-Slot oder im Bearbeiten-Dialog einer Spule (dort auch „umhängen“).
- **Restgewicht** = Restmenge % (AMS-Schätzung) × **Vollgewicht** des Tags (netto, Standard 1000 g, pro Tag änderbar).
- **Verbrauch** wird mitprotokolliert (nur Abnahmen), auch je Druck in der Druckhistorie.
- **„Spule leer“** im AMS-Slot: Bestand −1 und Tag freigeben, um ihn der nächsten Spule zuzuordnen.
- Voraussetzung für Füllstände: AMS-Option „Restmenge schätzen“ aktiv. Spulen ohne Tag liefern keinen Füllstand.
- Optional: „neue Original-Bambu-Spulen nach Bambu-Angabe zuordnen“ (Standard aus).

## Handy & App-Installation

- Volle PWA (Installieren-Knopf, Offline-Oberfläche) benötigt **HTTPS**, z. B. hinter einem Reverse Proxy (Caddy, Traefik, Nginx Proxy Manager).
- Über `http://IP:8095` funktioniert „Zum Home-Bildschirm hinzufügen“ ebenfalls, nur ohne Offline-Cache.
- Kurzbefehle: `/?tab=printer`, `/?tab=bambu`, `/?tab=filaments&view=check` (Inventur).

## Sicherheit

- Es gibt **keine Anmeldung** in der App → nur im LAN betreiben oder hinter einem Reverse Proxy mit Authentifizierung (Authelia, Authentik, Keycloak …).
- Bambu-Token und LAN-Zugangscodes liegen im Klartext in `data/inventar.db` → Verzeichnis entsprechend schützen.

## API (Auszug)

| Methode | Pfad | Zweck |
|---|---|---|
| GET/POST | `/api/filaments` | Filament lesen/anlegen |
| GET/PUT/PATCH/DELETE | `/api/filaments/<id>` | einzelne Spule |
| POST | `/api/filaments/<id>/adjust` | Bestand ändern `{"delta": -1}` |
| GET/POST | `/api/materials` | Material & Zubehör (`kind`: `material`/`zubehoer`) |
| GET | `/api/ams/status` | Druckerstatus aller Drucker |
| GET | `/api/ams/live` | AMS-Slots inkl. zugeordneter Spulen |
| GET | `/api/ams/rfid` | alle RFID-Tags (AMS + Cloud) |
| POST | `/api/ams/rfid/<tag>/link` | Tag zuordnen `{"filament_id": 12}` |
| GET | `/api/ams/jobs` | Druckhistorie mit Verbrauch |
| GET | `/api/export`, `/api/export.csv?kind=filaments` | Backup / CSV |

Damit lassen sich z. B. in Home Assistant per `rest`-Sensor Bestand, AMS-Belegung und Druckerstatus anzeigen.

## Technik

Python 3.12 · Flask · SQLite · gunicorn (1 Worker, Threads) · paho-mqtt · curl_cffi · Vanilla JS/CSS ohne Build-Schritt.

```
app/
  app.py              Inventar-API, Export/Import, Migrationen
  ams.py              MQTT-Verbindungen, AMS-Slots, RFID-Tags, Druckhistorie
  printer_status.py   Status-Dekodierung, HMS-Texte, Job-Tracking
  bambu.py            Bambu-Cloud: Login, Filament-Manager-Sync
  bambu_cloud.py      HTTP-Client für die Bambu-Cloud
  catalog/            Farbkataloge (Bambu, Community)
  hms/                HMS-Fehlertexte (de/en)
  static/             Oberfläche, PWA
```

## Haftungsausschluss

Die Bambu-Cloud-Schnittstelle ist inoffiziell (dieselbe, die Bambu Studio nutzt) und kann sich jederzeit ändern. Dieses Projekt ist nicht mit Bambu Lab verbunden. Quellen und Lizenzen der enthaltenen Daten: [THIRD_PARTY.md](THIRD_PARTY.md).

## Autor

**[Qu3ntyn83](https://github.com/Qu3ntyn83)** – Idee, Anforderungen und Test im eigenen Homelab (Bambu Lab H2S mit AMS 2 Pro).

## Lizenz

[MIT](LICENSE) – ausgenommen die in [THIRD_PARTY.md](THIRD_PARTY.md) genannten Dateien.
