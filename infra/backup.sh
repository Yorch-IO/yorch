#!/usr/bin/env bash
#
# Company Brain — take a backup of everything that cannot be regenerated for
# free, and verify that what the catalog claims to hold is actually on disk.
#
# What is backed up, in order of what it costs to lose:
#
#   1. The workspace artifacts (`runs/`, `profiles/`, `cache/`, `inbox/`).
#      These are the *only* copy of `chunks.jsonl` and `semantics.json`, and
#      they are what `rebuild` replays. Note where they live: bulk data never
#      enters a Temporal payload, so Postgres holds only an `ArtifactRef` —
#      a relative path, a sha256 and a size. **A database dump alone is not a
#      backup**; without these files a rebuild has nothing to replay and the
#      only way back is re-running correction, which is the expensive stage.
#   2. The Postgres catalog. The source of truth for provenance, costs and
#      version identity; Qdrant and Memgraph are projections of it.
#   3. The Qdrant collection, via its own snapshot API. Derived, but restoring
#      it by re-indexing costs embedding, and a snapshot costs nothing.
#   4. The Memgraph store, via its own `CREATE SNAPSHOT`. Also derived, and the
#      cheapest of the three to rebuild — kept because a file copy beats
#      re-projecting.
#
# Deliberately NOT backed up:
#
#   - `secrets.env` and `.env`. The first holds provider API keys, the second a
#     Postgres password. A backup is a copy that outlives the machine and gets
#     moved around; credentials must not ride along. The keychain owns the keys
#     and the app rewrites `.env` on every launch.
#   - The original documents at their own paths. They are the user's files, and
#     `inbox/` already holds what was staged.
#   - `workspace/snapshots/`, which is where Qdrant writes its own snapshots.
#     Backing that up inside the workspace tar would store each one twice.
#
# Consistency: the four legs are taken seconds apart, so a document indexed
# mid-backup can land in one and not another. That skew is repairable rather
# than corrupting — the catalog is the source of truth and reindex/rebuild
# converges onto it — but for a clean backup, stop the worker first:
#   docker compose -p company-brain -f docker-compose.yaml stop worker
#
# Usage:
#   ./backup.sh                 # take a backup into ~/company-brain-backups
#   ./backup.sh -o /mnt/disk    # somewhere else
#   ./backup.sh --verify        # check catalog vs disk, take nothing
#   ./backup.sh --keep 3        # prune to the 3 newest afterwards

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${HOME}/company-brain-backups"
KEEP=7
VERIFY_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--out)   DEST="$2"; shift 2 ;;
    --keep)     KEEP="$2"; shift 2 ;;
    --verify)   VERIFY_ONLY=1; shift ;;
    -h|--help)  sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "backup: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

# -- what the stack is, read from the same .env the app writes ---------------

[[ -f "${HERE}/.env" ]] || die "no ${HERE}/.env — start the stack once so the app writes it"
# shellcheck disable=SC1091
set -a; . "${HERE}/.env"; set +a

PROJECT="${BRAIN_PROJECT:-company-brain}"
WORKSPACE="${BRAIN_WORKSPACE:?BRAIN_WORKSPACE missing from .env}"
PG_USER="${BRAIN_PG_USER:-brain}"
API_PORT="${BRAIN_API_HOST_PORT:-8787}"
QDRANT_PORT="${BRAIN_QDRANT_HTTP_PORT:-6433}"
COLLECTION="${BRAIN_QDRANT_COLLECTION:-brain}"

[[ -d "$WORKSPACE" ]] || die "workspace $WORKSPACE does not exist"

# Containers are found by compose label rather than by name, so this keeps
# working whichever overlays were used and whatever the containers ended up
# being called.
container() {
  local id
  id="$(docker ps -q \
        --filter "label=com.docker.compose.project=${PROJECT}" \
        --filter "label=com.docker.compose.service=$1" | head -1)"
  [[ -n "$id" ]] || die "the '$1' container is not running — start the stack first"
  printf '%s' "$id"
}

PG="$(container postgres)"

# -- verify: does every artifact the catalog names still exist, unchanged? ---
#
# This is the check that would have caught eight pruned `chunks.jsonl` files
# before anybody needed them. It runs before every backup, because a backup of
# a workspace that has already lost files is a backup of the loss.

verify_artifacts() {
  local rows missing=0 changed=0 ok=0
  rows="$(docker exec "$PG" psql -U "$PG_USER" -d brain -tAF$'\t' \
            -c "select rel_path, sha256 from run_artifact order by rel_path;")" \
    || die "cannot read the catalog"

  while IFS=$'\t' read -r rel sha; do
    [[ -n "$rel" ]] || continue
    local file="${WORKSPACE}/${rel}"
    if [[ ! -f "$file" ]]; then
      missing=$((missing + 1)); say "  ausente  ${rel}"
    elif [[ "$(sha256sum "$file" | cut -d' ' -f1)" != "$sha" ]]; then
      changed=$((changed + 1)); say "  cambiado ${rel}"
    else
      ok=$((ok + 1))
    fi
  done <<< "$rows"

  say "artefactos: ${ok} íntegros, ${missing} ausentes, ${changed} con sha256 distinto"
  # A missing artifact is not a reason to refuse the backup — it is a reason to
  # say so loudly and back up what is left.
  [[ $missing -eq 0 && $changed -eq 0 ]] || say "AVISO: el catálogo nombra ficheros que ya no están o han cambiado."
}

say "== verificando el catálogo contra el disco =="
verify_artifacts
[[ $VERIFY_ONLY -eq 0 ]] || exit 0

# -- take it ----------------------------------------------------------------

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST}/${STAMP}"
mkdir -p "$OUT"
say ""
say "== respaldando en ${OUT} =="

say "-- catálogo (pg_dump)"
docker exec "$PG" pg_dump -U "$PG_USER" -d brain --no-owner --no-privileges \
  | gzip -6 > "${OUT}/catalog.sql.gz"

say "-- artefactos del workspace (tar)"
tar -C "$WORKSPACE" -czf "${OUT}/workspace.tar.gz" \
  --exclude=snapshots \
  $( [[ -d "${WORKSPACE}/runs"     ]] && echo runs ) \
  $( [[ -d "${WORKSPACE}/profiles" ]] && echo profiles ) \
  $( [[ -d "${WORKSPACE}/cache"    ]] && echo cache ) \
  $( [[ -d "${WORKSPACE}/inbox"    ]] && echo inbox )

say "-- Qdrant (snapshot API)"
SNAP="$(curl -sf -m 300 -X POST "http://127.0.0.1:${QDRANT_PORT}/collections/${COLLECTION}/snapshots" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])')" \
  || die "Qdrant did not take a snapshot on port ${QDRANT_PORT}"
# Downloaded over HTTP rather than read off the bind mount: Qdrant runs as root
# in its container, so the file it writes there is not always readable by the
# host user.
curl -sf -m 600 -o "${OUT}/qdrant-${COLLECTION}.snapshot" \
  "http://127.0.0.1:${QDRANT_PORT}/collections/${COLLECTION}/snapshots/${SNAP}" \
  || die "could not download the Qdrant snapshot ${SNAP}"
# Removed server-side so the workspace does not accumulate one per backup.
curl -sf -m 60 -X DELETE \
  "http://127.0.0.1:${QDRANT_PORT}/collections/${COLLECTION}/snapshots/${SNAP}" >/dev/null || true

# `CREATE SNAPSHOT` is a write to Memgraph's own durability history, not a read
# of it: the store keeps `--storage-snapshot-retention-count` of them and evicts
# the oldest. At the stock 3, three backups destroy every snapshot that predates
# them — which is how a pre-deletion snapshot was lost on 2026-08-21, 24 minutes
# after it was taken. docker-compose.yaml raises that count for this reason;
# check it before running backups in a tight loop.
say "-- Memgraph (CREATE SNAPSHOT)"
MG="$(container memgraph)"
MG_PATH="$(echo 'CREATE SNAPSHOT;' \
  | docker exec -i "$MG" mgconsole --host 127.0.0.1 --port 7687 --output-format csv \
  | tail -1 | tr -d '"')" || die "Memgraph refused to snapshot"
docker cp "${MG}:${MG_PATH}" "${OUT}/memgraph.snapshot" \
  || die "could not copy ${MG_PATH} out of the container"

# -- the manifest -----------------------------------------------------------
#
# Counts, not just files. A backup whose size looks right but holds an empty
# collection is the failure mode that matters, and only a count catches it.

say "-- manifiesto"
{
  echo "Company Brain backup"
  echo "taken_at_utc: ${STAMP}"
  echo "host: $(hostname)"
  echo "project: ${PROJECT}"
  echo "workspace: ${WORKSPACE}"
  echo "qdrant_collection: ${COLLECTION}"
  echo
  echo "[catalog rows]"
  docker exec "$PG" psql -U "$PG_USER" -d brain -tAc "
    select table_name || ': ' || (xpath('/row/c/text()',
      query_to_xml('select count(*) as c from ' || quote_ident(table_name), false, true, '')))[1]::text
    from information_schema.tables
    where table_schema='public' order by table_name;" 2>/dev/null || echo "  (unavailable)"
  echo
  echo "[qdrant]"
  curl -sf -m 30 "http://127.0.0.1:${QDRANT_PORT}/collections/${COLLECTION}" \
    | python3 -c 'import json,sys
r = json.load(sys.stdin)["result"]
print("  points:", r.get("points_count"))
print("  status:", r.get("status"))' 2>/dev/null || echo "  (unavailable)"
  echo
  echo "[memgraph nodes]"
  echo 'MATCH (n) RETURN labels(n) AS label, count(*) AS n ORDER BY n DESC;' \
    | docker exec -i "$MG" mgconsole --host 127.0.0.1 --port 7687 --output-format csv 2>/dev/null \
    | tail -n +2 | tr -d '"[]' | awk -F, '{printf "  %-16s %s\n", $1, $2}' || echo "  (unavailable)"
  echo
  echo "[api]"
  curl -sf -m 10 "http://127.0.0.1:${API_PORT}/openapi.json" \
    | python3 -c 'import json,sys; print("  routes:", len(json.load(sys.stdin)["paths"]))' 2>/dev/null \
    || echo "  (not answering — the backup is still valid, this is a note about the stack)"
  echo
  echo "[files]"
  # Everything but this file. Hashing the manifest from inside the redirect
  # that is still writing it yields the hash of a half-written file, which is
  # worse than no hash at all because it looks authoritative.
  ( cd "$OUT" && sha256sum -- catalog.sql.gz workspace.tar.gz \
      "qdrant-${COLLECTION}.snapshot" memgraph.snapshot )
  echo
  echo "[not included, deliberately]"
  echo "  secrets.env  provider API keys; a backup outlives the machine"
  echo "  .env         holds the Postgres password; rewritten by the app on every launch"
  echo "  the original documents at their own paths (inbox/ holds what was staged)"
} > "${OUT}/manifest.txt"

say ""
say "== hecho =="
du -sh "$OUT"
grep -E "^  (points|Chunk|artefactos)" "${OUT}/manifest.txt" 2>/dev/null || true

# -- retention --------------------------------------------------------------

if [[ "$KEEP" -gt 0 ]]; then
  mapfile -t old < <(ls -1d "${DEST}"/*/ 2>/dev/null | sort -r | tail -n +$((KEEP + 1)))
  for d in "${old[@]:-}"; do
    [[ -n "$d" ]] || continue
    say "prune: $d"
    rm -rf -- "$d"
  done
fi
