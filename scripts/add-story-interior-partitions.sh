#!/usr/bin/env bash
# add-story-interior-partitions.sh
#
# Adds the five story-interior world_partition rows that Funcom's
# initialize_partitions_full_battlegroup() creates but the DASH/self-host
# migration chain omits (same gap as AMP's migrate-db.sh; see
# docs/ecosystem-feature-parity-audit.md peer notes):
#
#   31 CB_Story_DestroyedZanovar        (Arrakeen Spaceport / Zanovar)
#   32 CB_Story_OrbitalMonitor          (Sardaukar Orbital Monitor)
#   33 CB_Arrakis_Story_Paranoid_PrayerRoom
#   34 CB_Arrakis_Story_Glutton_DiningRoom
#   35 CB_Arrakis_Generic_Sietch_Room   (Sietch Talab - Eighth Trial of Aql)
#
# IDs follow Funcom's world-template.yaml server-set ordering so future game
# migrations that reference partition ids see the canonical layout.
#
# NOTE: DASH's scripts/full-world-partitions.sh historically reserved id 31
# for an optional second Deep Desert dimension (DUNE_WORLD_PARTITION_COUNT=31).
# The official story maps take precedence. Enabling the second Deep Desert now
# requires remapping that row to a free id (>=36) first.
#
# Idempotent: uses the game's own dune.add_partition_unique() which skips
# existing (map, dimension) rows. Takes a pg_dump backup of world_partition
# before changing anything.
set -euo pipefail

env_file="${1:-.env}"
container_runtime="${CONTAINER_RUNTIME:-docker}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$script_dir/compose-files.sh" ]]; then
  COMPOSE_FILES="$("$script_dir/compose-files.sh" "$env_file")"
  export COMPOSE_FILES
fi
compose=("$container_runtime" compose)
IFS=':' read -ra compose_files <<< "${COMPOSE_FILES:-compose.yaml:compose.allmaps.yaml}"
for compose_file in "${compose_files[@]}"; do
  compose+=(-f "$compose_file")
done
compose+=(--env-file "$env_file")

read_env() {
  local key="$1" value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

db="$(read_env DUNE_GAME_DB_NAME)"
db="${db:-dune_sb_1_5_15_0}"
backup_dir=backups/partition-surgery
mkdir -p "$backup_dir"
backup_file="$backup_dir/world-partitions-before-story-interiors-$(date -u +%Y%m%dT%H%M%SZ).sql"

"${compose[@]}" exec -T postgres pg_dump \
  -U dune \
  -d "$db" \
  --schema=dune \
  --table=dune.world_partition \
  --data-only \
  > "$backup_file"
printf 'wrote backup: %s\n' "$backup_file"

"${compose[@]}" exec -T postgres psql -U dune -d "$db" -v ON_ERROR_STOP=1 <<'SQL'
begin;

-- Official world-template ordering (ids 31-35). add_partition_unique assigns
-- the next free id in call order and generates the canonical label via the
-- determine_partition_label trigger (DestroyedZanovar_0, OrbitalMonitor_0,
-- ParanoidRoom_0, GluttonRoom_0, SietchTalab_0).
select dune.add_partition_unique('CB_Story_DestroyedZanovar', '{"box": {"max_x": 1, "max_y": 1, "min_x": 0, "min_y": 0}, "type": "box2d_array"}'::jsonb, 0);
select dune.add_partition_unique('CB_Story_OrbitalMonitor', '{"box": {"max_x": 1, "max_y": 1, "min_x": 0, "min_y": 0}, "type": "box2d_array"}'::jsonb, 0);
select dune.add_partition_unique('CB_Arrakis_Story_Paranoid_PrayerRoom', '{"box": {"max_x": 1, "max_y": 1, "min_x": 0, "min_y": 0}, "type": "box2d_array"}'::jsonb, 0);
select dune.add_partition_unique('CB_Arrakis_Story_Glutton_DiningRoom', '{"box": {"max_x": 1, "max_y": 1, "min_x": 0, "min_y": 0}, "type": "box2d_array"}'::jsonb, 0);
select dune.add_partition_unique('CB_Arrakis_Generic_Sietch_Room', '{"box": {"max_x": 1, "max_y": 1, "min_x": 0, "min_y": 0}, "type": "box2d_array"}'::jsonb, 0);

select setval(
  'dune.world_partition_partition_id_seq',
  greatest(
    (select coalesce(max(partition_id), 1) from dune.world_partition),
    (select last_value from dune.world_partition_partition_id_seq)
  )
);

select dune.update_partition_labels(false);
notify world_partition_update;

commit;

select partition_id, map, dimension_index, label
from dune.world_partition
where partition_id >= 31
order by partition_id;
SQL
