#!/usr/bin/env bash
# Aufräumen nach Umstieg und NAS-Umzug – fragt vor jedem Löschen nach.
#
#   sudo bash scripts/cleanup.sh            # zeigt alles, fragt je Punkt
#   sudo DRY=1 bash scripts/cleanup.sh      # nur anzeigen, nichts löschen
#
# 1. Alte lokale Kopien:  data/*.vor-nas-*  (nur wenn die NAS-Kopie mindestens gleich viele Dateien hat),
#                         Override-Sicherungen, altes /opt/3d-management, Umstiegs-Archive in /root
# 2. Bibliothek:          verwaiste Dateien/Vorschaubilder, Einträge ohne Datei, fehlende Vorschauen,
#                         doppelte Dateien (getrennt gefragt)
# 3. Docker:              alter Container/Image „3d-management“, unbenutzte 3d-manager-Images,
#                         herrenlose Images, Build-Cache. Volumes werden nur aufgelistet, nie gelöscht.
#
# Variablen (optional): APP_DIR, OLD_DIR, PORT, CONTAINER, DRY
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/3d-manager}"
OLD_DIR="${OLD_DIR:-/opt/3d-management}"
PORT="${PORT:-8095}"
CONTAINER="${CONTAINER:-3d-manager}"
DRY="${DRY:-0}"
API="http://127.0.0.1:$PORT"

say()  { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '  · %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { [ "$DRY" = "1" ] && { info "(DRY – nichts gelöscht)"; return 1; }
         local a; read -rp "  $1 [j/N] " a </dev/tty; [[ "$a" =~ ^[jJyY]$ ]]; }
size() { du -sh "$1" 2>/dev/null | cut -f1; }
count(){ find "$1" -type f 2>/dev/null | wc -l; }

[ "$(id -u)" -eq 0 ] || die "Bitte mit sudo ausführen: sudo bash $0"
command -v docker >/dev/null || die "docker fehlt"
curl -fsS "$API/healthz" >/dev/null 2>&1 || die "3D Manager antwortet nicht auf $API – erst starten (cd $APP_DIR && sudo docker compose up -d)"
ok "3D Manager läuft"
FREE_BEFORE=$(df -B1 --output=avail / | tail -1)

# Wohin zeigen /library und /backups im laufenden Container?
mount_src() { docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$1\"}}{{.Source}}{{end}}{{end}}" "$CONTAINER" 2>/dev/null || true; }
LIVE_LIB="$(mount_src /library)"; LIVE_BAK="$(mount_src /backups)"

# ------------------------------------------------------------------ 1. alte Kopien
say "1/3 Alte lokale Kopien"
found=0
for d in "$APP_DIR"/data/library.vor-nas-* "$APP_DIR"/data/backups.vor-nas-*; do
  [ -d "$d" ] || continue
  found=1
  kind="$(basename "$d" | cut -d. -f1)"
  live="$LIVE_LIB"; [ "$kind" = "backups" ] && live="$LIVE_BAK"
  a=$(count "$d")
  if [ -z "$live" ]; then
    warn "$d ($a Dateien, $(size "$d")) – Container nutzt kein /$kind auf dem NAS, bleibt liegen"
    continue
  fi
  if [ ! -d "$live" ]; then
    warn "$d – NAS-Ziel $live nicht erreichbar, bleibt liegen"; continue
  fi
  b=$(count "$live")
  # Backups rotieren – dort reicht, dass die neueste lokale Sicherung auch auf dem NAS liegt
  if [ "$kind" = "backups" ]; then
    newest="$(ls -1t "$d" 2>/dev/null | head -1)"
    if [ -n "$newest" ] && [ ! -e "$live/$newest" ] && [ "$b" -lt "$a" ]; then
      warn "$d – $newest fehlt auf dem NAS, bleibt liegen"; continue
    fi
  elif [ "$b" -lt "$a" ]; then
    warn "$d – NAS hat nur $b von $a Dateien, bleibt liegen"; continue
  fi
  info "$d: $a Dateien, $(size "$d") – NAS ($live) hat $b"
  ask "löschen?" && { rm -rf "$d"; ok "gelöscht"; }
done
for f in "$APP_DIR"/docker-compose.override.yml.vor-nas-*; do
  [ -f "$f" ] || continue; found=1
  info "$f (Sicherung der Override-Datei vor dem NAS-Umzug)"
  ask "löschen?" && { rm -f "$f"; ok "gelöscht"; }
done
if [ -d "$OLD_DIR" ]; then
  found=1
  if [ -f "$APP_DIR/data/inventar.db" ] && ! docker ps --format '{{.Names}}' | grep -qx 3d-management; then
    info "$OLD_DIR – altes Paket „3D Management“, $(size "$OLD_DIR") (Daten sind übernommen)"
    ask "löschen?" && { rm -rf "$OLD_DIR"; ok "gelöscht"; }
  else
    warn "$OLD_DIR – altes Paket läuft noch oder Umstieg unvollständig, bleibt liegen"
  fi
fi
for f in /root/3d-management-vor-umstieg-*.tar.gz; do
  [ -f "$f" ] || continue; found=1
  info "$f ($(size "$f")) – Archiv vom Umstieg"
  ask "löschen? (nur wenn der 3D Manager seit dem Umstieg fehlerfrei läuft)" && { rm -f "$f"; ok "gelöscht"; }
done
[ "$found" = 1 ] || ok "nichts gefunden"

# ------------------------------------------------------------------ 2. Bibliothek
say "2/3 Bibliothek"
REP="$(curl -fsS "$API/api/library/cleanup")" || die "Bibliotheksprüfung fehlgeschlagen – ist der 3D Manager aktuell (git pull)?"
python3 - "$REP" <<'PY'
import json, sys
r = json.loads(sys.argv[1]); c = r["counts"]
mb = lambda xs: sum(x.get("size") or 0 for x in xs) / 1048576
rows = [("Dateien ohne Eintrag", c["orphan_files"], f"{mb(r['orphan_files']):.1f} MB", "werden gelöscht"),
        ("Vorschaubilder ohne Eintrag", c["orphan_thumbs"], f"{mb(r['orphan_thumbs']):.1f} MB", "werden gelöscht"),
        ("Einträge ohne Datei", c["missing_files"], "", "Eintrag wird entfernt"),
        ("fehlende Vorschauen", c["missing_thumbs"], "", "werden neu erzeugt"),
        ("doppelte Dateien", sum(len(g["remove"]) for g in r["duplicates"]), f"{sum(mb(g['remove']) for g in r['duplicates']):.1f} MB", "nur auf eigene Frage"),
        ("Modelle ohne Datei", c["empty_models"], "", "nur Hinweis")]
for name, n, s, what in rows:
    print(f"  · {name:28} {n:>4}  {s:>9}  {what if n else ''}")
for x in r["missing_files"][:10]:
    print(f"      fehlt: {x['name']} (Modell {x['model_id']})")
for g in r["duplicates"][:10]:
    print(f"      doppelt: {g['keep']['name']} (Modell {g['keep']['model_id']}) ← " + ", ".join(f"Modell {x['model_id']}" for x in g["remove"]))
for m in r["empty_models"][:10]:
    print(f"      leer: {m['title']} (#{m['id']})")
PY
fix=$(python3 -c 'import json,sys;c=json.loads(sys.argv[1])["counts"];print(c["orphan_files"]+c["orphan_thumbs"]+c["missing_files"]+c["missing_thumbs"])' "$REP")
dup=$(python3 -c 'import json,sys;print(sum(len(g["remove"]) for g in json.loads(sys.argv[1])["duplicates"]))' "$REP")
if [ "$fix" -gt 0 ] && ask "Bibliothek bereinigen (ohne Duplikate)?"; then
  curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' "$API/api/library/cleanup" \
    | python3 -c 'import json,sys;r=json.load(sys.stdin);print("  ✓ bereinigt, %.1f MB frei" % (r["freed_bytes"]/1048576))'
fi
if [ "$dup" -gt 0 ]; then
  info "Doppelte Dateien sind oft gewollt (gleiche Datei in zwei Modellen). Die älteste bleibt, das Modell selbst bleibt erhalten."
  if ask "$dup doppelte Datei(en) entfernen?"; then
    curl -fsS -X POST -H 'Content-Type: application/json' -d '{"duplicates":true}' "$API/api/library/cleanup" \
      | python3 -c 'import json,sys;r=json.load(sys.stdin);print("  ✓ entfernt, %.1f MB frei" % (r["freed_bytes"]/1048576))'
  fi
fi
[ "$fix" -eq 0 ] && [ "$dup" -eq 0 ] && ok "Bibliothek ist sauber"

# ------------------------------------------------------------------ 3. Docker
say "3/3 Docker"
docker system df | sed 's/^/  /'
if docker ps -a --format '{{.Names}}' | grep -qx 3d-management; then
  info "Container 3d-management ($(docker ps -a --filter name=^3d-management$ --format '{{.Status}}'))"
  ask "entfernen?" && { docker rm -f 3d-management >/dev/null; ok "entfernt"; }
fi
OLD_IMG="$(docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -E '(^|/)3d-management[: ]' || true)"
if [ -n "$OLD_IMG" ]; then
  echo "$OLD_IMG" | sed 's/^/  · altes Image: /'
  ask "entfernen?" && { echo "$OLD_IMG" | awk '{print $2}' | sort -u | xargs -r docker image rm -f >/dev/null && ok "entfernt"; }
fi
USED="$(docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null | sed 's/^sha256://' | cut -c1-12)"
STALE="$(docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' \
  | grep -E '3d-manager' | grep -v "sha256:$USED" || true)"
if [ -n "$STALE" ]; then
  echo "$STALE" | sed 's/sha256:\(.\{12\}\)[0-9a-f]*/\1/' | sed 's/^/  · alte 3d-manager-Version: /'
  ask "entfernen?" && { echo "$STALE" | awk '{print $2}' | sort -u | xargs -r docker image rm -f >/dev/null 2>&1; ok "entfernt"; }
fi
DANG=$(docker images -f dangling=true -q | wc -l)
if [ "$DANG" -gt 0 ]; then
  info "$DANG herrenlose Images (<none>, von früheren Updates/Builds)"
  ask "entfernen?" && { docker image prune -f | tail -1 | sed 's/^/  ✓ /'; }
fi
CACHE="$(docker system df --format '{{.Type}} {{.Size}}' | awk '/Build Cache/{print $3}')"
if [ -n "$CACHE" ] && [ "$CACHE" != "0B" ]; then
  info "Build-Cache: $CACHE"
  ask "leeren?" && { docker builder prune -af | tail -1 | sed 's/^/  ✓ /'; }
fi
VOL="$(docker volume ls -qf dangling=true)"
if [ -n "$VOL" ]; then
  info "Unbenutzte Volumes (werden NICHT gelöscht – können zu gestoppten Diensten gehören):"
  echo "$VOL" | sed 's/^/      /'
  info "bei Bedarf einzeln: sudo docker volume rm <name>"
fi

FREE_AFTER=$(df -B1 --output=avail / | tail -1)
say "Fertig"
ok "frei auf /: $(numfmt --to=iec $FREE_AFTER) ($(numfmt --to=iec $(( FREE_AFTER - FREE_BEFORE > 0 ? FREE_AFTER - FREE_BEFORE : 0 ))) gewonnen)"
