#!/usr/bin/env bash
# prune-game-logs.sh — bounded retention for DASH game-log sinks.
#
# Two growth sources, neither covered by Docker's json-file log rotation or by
# scripts/storage-cleanup.sh (which is scoped to obsolete images and explicitly
# never touches data):
#
#   1. Director file sink (in-container): since game build 2160922 the director
#      writes daily-rotated logs to
#      /Tools/Battlegroups/Director/BattlegroupDirector/logs/directorYYYYMMDD.log
#      (~95MB/day). scripts/fls-publication-health.py tails the two newest of
#      these files for FLS publication checks.
#   2. Map container UE5 logs (host bind mounts): data/server-saved/<map>/Logs/.
#
# Defaults to dry-run. Deletion requires --execute. The two newest director log
# files are always protected (active file + UTC-midnight rotation boundary),
# independent of age.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAYS=7
EXECUTE=false
DIRECTOR_CONTAINER="${DUNE_DIRECTOR_CONTAINER:-duneawakeningselfhost-director-1}"
DIRECTOR_LOG_DIR="/Tools/Battlegroups/Director/BattlegroupDirector/logs"
MAP_LOG_GLOB="$ROOT_DIR/data/server-saved"/*/Logs

usage() {
  cat <<'EOF'
Usage:
  scripts/prune-game-logs.sh [--days N]            dry-run (default)
  scripts/prune-game-logs.sh [--days N] --execute  delete candidates

Retention: deletes director file-sink logs and map UE5 logs older than N days
(default 7). The two newest director log files are never deleted (the health
check tails them). Dry-run prints candidates and reclaimable bytes only.
EOF
}

while (($#)); do
  case "$1" in
    --days)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "--days requires a non-negative integer" >&2; exit 2; }
      DAYS="$1"
      ;;
    --execute) EXECUTE=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mode="DRY-RUN"
[[ "$EXECUTE" == true ]] && mode="EXECUTE"
echo "[$mode] prune-game-logs: retention ${DAYS}d  $(date -u +%Y-%m-%dT%H:%M:%SZ)"

total_bytes=0
total_files=0

# --- 1. director container file sink ---------------------------------------
if docker inspect -f '{{.State.Running}}' "$DIRECTOR_CONTAINER" 2>/dev/null | grep -qx true; then
  # List all rotated logs (name-sorted == date-sorted); protect the 2 newest.
  mapfile -t all_logs < <(docker exec "$DIRECTOR_CONTAINER" sh -c \
    "ls -1 $DIRECTOR_LOG_DIR/director2*.log 2>/dev/null" || true)
  protected=()
  if ((${#all_logs[@]} > 0)); then
    protected=("${all_logs[@]: -2}")
  fi
  candidates=()
  if ((${#all_logs[@]} > 0)); then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      skip=false
      for p in ${protected[@]+"${protected[@]}"}; do
        [[ "$f" == "$p" ]] && skip=true && break
      done
      $skip || candidates+=("$f")
    done < <(docker exec "$DIRECTOR_CONTAINER" sh -c \
      "find $DIRECTOR_LOG_DIR -maxdepth 1 -type f -name 'director2*.log' -mtime +$DAYS 2>/dev/null" || true)
  fi
  for f in ${candidates[@]+"${candidates[@]}"}; do
    size=$(docker exec "$DIRECTOR_CONTAINER" sh -c "stat -c%s '$f' 2>/dev/null" || echo 0)
    total_bytes=$((total_bytes + ${size:-0}))
    total_files=$((total_files + 1))
    if [[ "$EXECUTE" == true ]]; then
      docker exec "$DIRECTOR_CONTAINER" rm -f "$f" && echo "REMOVED (director) $f ($size bytes)"
    else
      echo "WOULD REMOVE (director) $f ($size bytes)"
    fi
  done
  ((${#candidates[@]} > 0)) || echo "director sink: no candidates (${#all_logs[@]} files present, newest 2 protected)"
else
  echo "director sink: container $DIRECTOR_CONTAINER not running — skipped"
fi

# --- 2. host map UE5 logs ----------------------------------------------------
shopt -s nullglob
map_log_dirs=( $MAP_LOG_GLOB )
shopt -u nullglob
if ((${#map_log_dirs[@]} > 0)); then
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    size=$(sudo -n stat -c%s "$f" 2>/dev/null || echo 0)
    total_bytes=$((total_bytes + ${size:-0}))
    total_files=$((total_files + 1))
    if [[ "$EXECUTE" == true ]]; then
      sudo -n rm -f "$f" && echo "REMOVED (map) $f ($size bytes)"
    else
      echo "WOULD REMOVE (map) $f ($size bytes)"
    fi
  done < <(sudo -n find "${map_log_dirs[@]}" -type f -name '*.log' -mtime +"$DAYS" 2>/dev/null)
else
  echo "map logs: no data/server-saved/*/Logs directories — skipped"
fi

printf '[%s] %d candidate file(s), %d bytes reclaimable\n' "$mode" "$total_files" "$total_bytes"
