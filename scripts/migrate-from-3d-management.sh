#!/usr/bin/env bash
# Umstieg vom alten Paket „3D Management“ auf den 3D Manager – mit Übernahme aller Daten.
#
#   curl -fsSL https://raw.githubusercontent.com/Qu3ntyn83/3D-Manager/main/scripts/migrate-from-3d-management.sh -o migrate.sh
#   sudo bash migrate.sh
#
# Ablauf: alten Container stoppen → Daten sichern → 3D Manager klonen → Daten kopieren → starten → prüfen.
# Das alte Verzeichnis bleibt unverändert liegen (Rückweg siehe Ende).
#
# Variablen (optional): OLD_DIR, NEW_DIR, BRANCH, REPO, PORT
set -euo pipefail

OLD_DIR="${OLD_DIR:-/opt/3d-management}"
NEW_DIR="${NEW_DIR:-/opt/3d-manager}"
REPO="${REPO:-https://github.com/Qu3ntyn83/3D-Manager.git}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8095}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${BACKUP:-/root/3d-management-vor-umstieg-$STAMP.tar.gz}"

say()  { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo ausführen: sudo bash $0"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; elif command -v docker-compose >/dev/null; then DC="docker-compose"; else die "docker compose fehlt"; fi
command -v git >/dev/null || die "git fehlt (apt install git)"

say "Prüfe altes Paket in $OLD_DIR"
[ -f "$OLD_DIR/data/inventar.db" ] || die "$OLD_DIR/data/inventar.db nicht gefunden – OLD_DIR=/pfad setzen"
ok "Datenbank gefunden ($(du -h "$OLD_DIR/data/inventar.db" | cut -f1)), Fotos: $(find "$OLD_DIR/data/uploads" -type f 2>/dev/null | wc -l)"
if [ -f "$NEW_DIR/data/inventar.db" ] && [ "${FORCE:-0}" != "1" ]; then
  die "$NEW_DIR/data/inventar.db existiert schon – Umstieg wurde wohl bereits gemacht (FORCE=1 überschreibt)"
fi

say "Stoppe alten Container"
if [ -f "$OLD_DIR/docker-compose.yml" ]; then (cd "$OLD_DIR" && $DC down) || true; fi
docker rm -f 3d-management >/dev/null 2>&1 || true
ok "3d-management gestoppt"

say "Sichere alte Daten nach $BACKUP"
tar -czf "$BACKUP" -C "$OLD_DIR" data
ok "$(du -h "$BACKUP" | cut -f1)"

say "Hole 3D Manager ($BRANCH) nach $NEW_DIR"
if [ -d "$NEW_DIR/.git" ]; then
  git -C "$NEW_DIR" fetch --quiet origin "$BRANCH" && git -C "$NEW_DIR" checkout --quiet "$BRANCH" && git -C "$NEW_DIR" pull --quiet --ff-only
else
  git clone --quiet --branch "$BRANCH" "$REPO" "$NEW_DIR"
fi
ok "$(git -C "$NEW_DIR" log -1 --format='%h %s')"

say "Übernehme Daten"
rm -rf "$NEW_DIR/data"
cp -a "$OLD_DIR/data" "$NEW_DIR/data"
chown -R 1000:1000 "$NEW_DIR/data"
ok "Datenbank + Fotos kopiert"

# DNS-Einstellung des alten Pakets übernehmen (sonst „Could not resolve host“ bei der Bambu-Cloud)
DNS="$(awk '/^ *dns:/{f=1;next} f&&/^ *- /{gsub(/[- "]/,"");print;next} f{exit}' "$OLD_DIR/docker-compose.yml" 2>/dev/null || true)"
OVR=""
if [ "$PORT" != "8095" ]; then OVR+=$'    ports: !override\n      - "'"$PORT"$':8080"\n'; fi
if [ -n "$DNS" ]; then OVR+=$'    dns:\n'; for d in $DNS; do OVR+="      - $d"$'\n'; done; fi
if [ -n "$OVR" ]; then
  printf '# Lokale Anpassungen (beim Umstieg erzeugt, bleibt bei git pull erhalten)\nservices:\n  3d-manager:\n%s' "$OVR" > "$NEW_DIR/docker-compose.override.yml"
  ok "docker-compose.override.yml angelegt (DNS: $(echo $DNS))"
fi

say "Starte 3D Manager"
cd "$NEW_DIR"
$DC up -d --build
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 2
  [ "$i" -eq 60 ] && { $DC logs --tail 50; die "Start fehlgeschlagen – Logs oben. Rückweg siehe unten im Skript."; }
done
ok "läuft auf http://$(hostname -I | awk '{print $1}'):$PORT"

say "Prüfe übernommene Daten"
python3 - "$PORT" <<'PY' || curl -fsS "http://127.0.0.1:$PORT/api/stats" | head -c 400
import json, sys, urllib.request
p = sys.argv[1]
get = lambda u: json.load(urllib.request.urlopen(f"http://127.0.0.1:{p}{u}"))
f = get("/api/filaments"); m = get("/api/materials"); t = get("/api/ams/rfid")
units = sum(len(x.get("units", [])) for x in f)
print(f"  Filament-Sorten: {len(f)} · Spulen: {units} · Material/Zubehör: {len(m)}")
print(f"  RFID-Tags: {len(t)}, davon zugeordnet: {sum(1 for x in t if x.get('filament_ref'))}")
PY

cat <<EOF

Fertig. Alte Daten gesichert in: $BACKUP
Das alte Verzeichnis $OLD_DIR ist unverändert und kann nach ein paar Tagen weg:
  sudo rm -rf $OLD_DIR && docker image rm 3d-management:latest

Rückweg (falls etwas nicht passt):
  cd $NEW_DIR && sudo $DC down
  cd $OLD_DIR && sudo $DC up -d

Updates künftig:
  cd $NEW_DIR && sudo git pull && sudo $DC up -d --build
EOF
