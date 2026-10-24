#!/usr/bin/env bash
# Druckdateien (Bibliothek) und Backups des 3D Manager auf eine SMB-Freigabe (NAS) umziehen.
#
#   sudo bash scripts/move-storage-to-smb.sh
#
# Standard: \\192.168.178.241\Docker\3D-Manager  →  library/ und backups/
# Die Datenbank (data/inventar.db) bleibt lokal: SQLite über SMB ist nicht sperrsicher.
# Ihre täglichen Sicherungen liegen danach aber mit auf dem NAS.
#
# Variablen (optional): NAS, SHARE, SUBDIR, APP_DIR, MNT, SMB_USER, SMB_VERS
set -euo pipefail

NAS="${NAS:-192.168.178.241}"
SHARE="${SHARE:-Docker}"
SUBDIR="${SUBDIR:-3D-Manager}"
APP_DIR="${APP_DIR:-/opt/3d-manager}"
MNT="${MNT:-/mnt/qnap/docker}"
CRED="${CRED:-/root/.smb-${NAS//./-}-${SHARE,,}}"
APP_UID="${APP_UID:-1000}"
PORT="${PORT:-8095}"
STAMP="$(date +%Y%m%d-%H%M%S)"

say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Override-Datei ergänzen, vorhandene Einträge (z. B. dns) behalten
write_override() {
  local f="$1" lib="$2" bak="$3"
  local block_env block_vol
  block_env=$'    environment:\n      - LIBRARY_DIR=/library\n      - BACKUP_DIR=/backups\n'
  block_vol=$'    volumes:\n      - type: bind\n        source: '"$lib"$'\n        target: /library\n        bind:\n          create_host_path: false\n      - type: bind\n        source: '"$bak"$'\n        target: /backups\n        bind:\n          create_host_path: false\n'
  if [ -f "$f" ]; then
    grep -q "LIBRARY_DIR=/library" "$f" && { ok "Override enthält die NAS-Ablage bereits"; return 0; }
    grep -qE '^    (environment|volumes):' "$f" && die "$f hat schon environment:/volumes: – bitte von Hand ergänzen (siehe README)"
    grep -q '^services:' "$f" && grep -q '^  3d-manager:' "$f" || die "$f hat ein unbekanntes Format – bitte von Hand ergänzen"
    cp -a "$f" "$f.vor-nas-$STAMP"
    printf '%s%s' "$block_env" "$block_vol" >> "$f"
  else
    printf '# Lokale Anpassungen (bleibt bei git pull erhalten)\nservices:\n  3d-manager:\n%s%s' "$block_env" "$block_vol" > "$f"
  fi
}
[ "${SOURCE_ONLY:-0}" = "1" ] && return 0 2>/dev/null

[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo ausführen: sudo bash $0"
[ -f "$APP_DIR/docker-compose.yml" ] || die "$APP_DIR/docker-compose.yml nicht gefunden (APP_DIR=/pfad setzen)"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else die "docker compose fehlt"; fi

say "Zugangsdaten für //$NAS/$SHARE"
if [ ! -s "$CRED" ]; then
  read -rp "  NAS-Benutzer: " u
  read -rsp "  Passwort: " p; echo
  umask 077
  printf 'username=%s\npassword=%s\n' "$u" "$p" > "$CRED"
  chmod 600 "$CRED"
  ok "gespeichert in $CRED (nur root lesbar)"
else
  ok "vorhanden: $CRED"
fi

say "CIFS-Unterstützung"
command -v mount.cifs >/dev/null || { apt-get update -qq && apt-get install -y -qq cifs-utils >/dev/null; }
ok "cifs-utils installiert"

say "Freigabe dauerhaft einhängen unter $MNT"
UNIT="$(systemd-escape --path "$MNT").mount"
mkdir -p "$MNT"
OPTS="credentials=$CRED,uid=$APP_UID,gid=$APP_UID,file_mode=0664,dir_mode=0775,iocharset=utf8,noserverino${SMB_VERS:+,vers=$SMB_VERS}"
cat > "/etc/systemd/system/$UNIT" <<EOF
[Unit]
Description=QNAP-Freigabe //$NAS/$SHARE für Docker
Wants=network-online.target
After=network-online.target

[Mount]
What=//$NAS/$SHARE
Where=$MNT
Type=cifs
Options=$OPTS
TimeoutSec=30

[Install]
WantedBy=multi-user.target
EOF
# Docker erst nach dem NAS starten, damit der Container die Freigabe sieht
mkdir -p /etc/systemd/system/docker.service.d
printf '[Unit]\nWants=%s\nAfter=%s\n' "$UNIT" "$UNIT" > /etc/systemd/system/docker.service.d/10-nas-${SHARE,,}.conf
systemctl daemon-reload
systemctl enable --now "$UNIT" || { journalctl -u "$UNIT" -n 20 --no-pager; die "Einhängen fehlgeschlagen – Benutzer/Passwort ($CRED) und Freigabename prüfen"; }
mountpoint -q "$MNT" || die "$MNT ist nicht eingehängt"
ok "$(df -h "$MNT" | tail -1 | awk '{print $2" gesamt, "$4" frei"}')"

say "Zielordner"
LIB="$MNT/$SUBDIR/library"; BAK="$MNT/$SUBDIR/backups"
mkdir -p "$LIB" "$BAK"
t="$LIB/.schreibtest-$STAMP"
setpriv --reuid="$APP_UID" --regid="$APP_UID" --clear-groups sh -c "touch '$t' && rm '$t'" || die "Kein Schreibrecht auf $LIB – Rechte auf der QNAP prüfen"
ok "$LIB"; ok "$BAK"

say "3D Manager anhalten"
cd "$APP_DIR"
$DC stop >/dev/null 2>&1 || true
ok "gestoppt"

say "Dateien kopieren"
command -v rsync >/dev/null || apt-get install -y -qq rsync >/dev/null
for pair in "library:$LIB" "backups:$BAK"; do
  src="$APP_DIR/data/${pair%%:*}"; dst="${pair#*:}"
  if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
    rsync -rt --info=stats1 "$src/" "$dst/" | grep -E "Number of regular files transferred|Total transferred file size" | sed 's/^/  /'
    a=$(find "$src" -type f | wc -l); b=$(find "$dst" -type f | wc -l)
    [ "$b" -ge "$a" ] || die "Kopie unvollständig ($b von $a Dateien) – nichts verändert, 3D Manager mit '$DC start' wieder starten"
    mv "$src" "$src.vor-nas-$STAMP"
    ok "${pair%%:*}: $a Dateien kopiert, Original → data/${pair%%:*}.vor-nas-$STAMP"
  else
    ok "${pair%%:*}: nichts zu kopieren"
  fi
done

say "docker-compose.override.yml"
write_override "$APP_DIR/docker-compose.override.yml" "$LIB" "$BAK"
ok "Bibliothek → /library, Backups → /backups"

say "3D Manager starten"
$DC up -d >/dev/null
for i in $(seq 1 45); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 2; [ "$i" -eq 45 ] && { $DC logs --tail 40; die "Start fehlgeschlagen"; }; done
python3 - "$PORT" <<'PY' || true
import json, sys, urllib.request
p = sys.argv[1]
g = lambda u: json.load(urllib.request.urlopen(f"http://127.0.0.1:{p}{u}"))
s, b = g("/api/library/stats"), g("/api/backups")
print(f"  Bibliothek: {s['dir']} · {s['models']} Modelle, {s['files']} Dateien")
print(f"  Backups:    {b['dir']} · {len(b['files'])} Archive")
PY

cat <<EOF

Fertig. Druckdateien und Backups liegen jetzt auf \\\\$NAS\\$SHARE\\$SUBDIR.
Die Originale bleiben vorerst unter $APP_DIR/data/*.vor-nas-$STAMP – nach ein paar Tagen löschen:
  sudo rm -rf $APP_DIR/data/library.vor-nas-$STAMP $APP_DIR/data/backups.vor-nas-$STAMP

Hinweis Backrest: Die Freigabe "$SHARE" ist bisher nicht im Backup nach D-Syno – dort als Pfad ergänzen.

Rückweg:
  cd $APP_DIR && sudo $DC stop
  sudo mv data/library.vor-nas-$STAMP data/library; sudo mv data/backups.vor-nas-$STAMP data/backups
  sudo mv docker-compose.override.yml.vor-nas-$STAMP docker-compose.override.yml   # falls vorhanden, sonst Datei löschen
  sudo $DC up -d
EOF
