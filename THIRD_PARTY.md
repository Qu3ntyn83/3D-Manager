# Drittquellen

Diese Dateien stammen aus fremden Projekten und stehen unter deren eigener Lizenz (nicht unter der MIT-Lizenz dieses Repositories):

| Datei | Quelle | Lizenz |
|---|---|---|
| `app/catalog/bambu.json` (**nicht im Repository**) | Wird beim Docker-Build bzw. beim ersten Start von `app/catalog_fetch.py` aus `resources/profiles/BBL/filament/filaments_color_codes.json` von [Bambu Studio](https://github.com/bambulab/BambuStudio) geladen und umgewandelt (Farbcodes, Namen, IDs) | AGPL-3.0 |
| `app/catalog/studio_presets.json` (**nicht im Repository**) | Filamentprofil-Liste (Name, filament_id, Typ, Temperaturen) aus `resources/profiles/BBL` von [Bambu Studio](https://github.com/bambulab/BambuStudio), beim Build/Start von `app/catalog_fetch.py` geladen | AGPL-3.0 |
| `app/catalog/community.json` | Filament-Farbdatenbank von [color-tools / color-match-tools](https://github.com/dterracino/color_tools) | MIT |
| `app/hms/hms_de.json.gz`, `app/hms/hms_en.json.gz` | HMS-/Fehlertexte aus [ha-bambulab](https://github.com/greghesp/ha-bambulab) | MIT (siehe `app/hms/LICENSE-ha-bambulab`) |
| `app/static/vendor/qrcode.js` | [qrcode-generator](https://github.com/kazuhikoarase/qrcode-generator) von Kazuhiko Arase | MIT |
| `app/static/vendor/jsQR.js` | [jsQR](https://github.com/cozmo/jsQR) | Apache-2.0 (siehe `app/static/vendor/LICENSE-jsQR`) |

Schlüsselableitung und Blockformat der Bambu-RFID-Tags (`android/…/BambuTag.java`) folgen dem [RFID-Tag-Guide der Bambu Research Group](https://github.com/Bambu-Research-Group/RFID-Tag-Guide).

Die Kommunikation mit der Bambu-Cloud und dem Drucker (MQTT) orientiert sich an der Dokumentation und dem Code von [ha-bambulab](https://github.com/greghesp/ha-bambulab), [ha-bambu-filaments](https://github.com/Timmes123/ha-bambu-filaments) und [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI).

„Bambu Lab“, „Bambu Studio“ und „AMS“ sind Marken von Bambu Lab. Dieses Projekt ist nicht mit Bambu Lab verbunden.
