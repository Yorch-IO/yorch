#!/usr/bin/env bash
#
# Company Brain — restore from a backup taken by ./backup.sh, or check that one
# is restorable without touching anything live.
#
# Read this before using it: restoring is destructive and the three stores are
# not equal.
#
#   --check      Validates a backup against throwaway targets: the catalog dump
#                is replayed into a scratch database, the Qdrant snapshot into a
#                scratch collection, the tar is listed and extracted to a temp
#                directory. Nothing live is touched, and all three are dropped
#                afterwards. **Run this before trusting a backup**, and after
#                copying one to another machine.
#
#   --catalog    Replaces the `brain` database. This is the source of truth, so
#                it is also the leg that decides what the other two mean.
#   --workspace  Unpacks runs/, profiles/, cache/, inbox/ over the workspace.
#                Additive by default: it does not delete files the backup lacks,
#                because a workspace usually has newer runs worth keeping.
#   --qdrant     Replaces the collection from its snapshot.
#   --memgraph   Replaces the graph store. **This is the one leg that has never
#                been run against a live stack.** The graph is a projection of
#                the catalog, so the tested way back is a Rebuild per document,
#                which re-projects it from `chunks.jsonl` and pays only for
#                embedding. Prefer that.
#   --all        catalog + workspace + qdrant. Not memgraph, for the reason above.
#
# Order matters and is enforced: catalog and workspace first, projections after.
# Removal writes projections first and the catalog last so a crash stays
# retryable; restoring is the same argument run backwards.
#
# Usage:
#   ./restore.sh --from ~/company-brain-backups/20260821T142729Z --check
#   ./restore.sh --from ~/company-brain-backups/20260821T142729Z --all --yes

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FROM=""
YES=0
CHECK=0
DO_CATALOG=0; DO_WORKSPACE=0; DO_QDRANT=0; DO_MEMGRAPH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)      FROM="$2"; shift 2 ;;
    --check)     CHECK=1; shift ;;
    --yes)       YES=1; shift ;;
    --catalog)   DO_CATALOG=1; shift ;;
    --workspace) DO_WORKSPACE=1; shift ;;
    --qdrant)    DO_QDRANT=1; shift ;;
    --memgraph)  DO_MEMGRAPH=1; shift ;;
    --all)       DO_CATALOG=1; DO_WORKSPACE=1; DO_QDRANT=1; shift ;;
    -h|--help)   sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "restore: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

[[ -n "$FROM" ]] || die "--from <backup directory> is required"
[[ -d "$FROM" ]] || die "$FROM is not a directory"
[[ -f "${FROM}/manifest.txt" ]] || die "$FROM has no manifest.txt — not a backup taken by backup.sh"

[[ -f "${HERE}/.env" ]] || die "no ${HERE}/.env"
# shellcheck disable=SC1091
set -a; . "${HERE}/.env"; set +a

PROJECT="${BRAIN_PROJECT:-company-brain}"
WORKSPACE="${BRAIN_WORKSPACE:?BRAIN_WORKSPACE missing from .env}"
PG_USER="${BRAIN_PG_USER:-brain}"
QDRANT_PORT="${BRAIN_QDRANT_HTTP_PORT:-6433}"
COLLECTION="${BRAIN_QDRANT_COLLECTION:-brain}"

container() {
  local id
  id="$(docker ps -q \
        --filter "label=com.docker.compose.project=${PROJECT}" \
        --filter "label=com.docker.compose.service=$1" | head -1)"
  [[ -n "$id" ]] || die "the '$1' container is not running — start the stack first"
  printf '%s' "$id"
}

# -- integrity of the backup itself -----------------------------------------

say "== comprobando el manifiesto =="
( cd "$FROM" && sed -n '/^\[files\]/,/^$/p' manifest.txt | grep -E '^[0-9a-f]{64} ' \
    | sha256sum -c --quiet - ) \
  || die "the recorded sha256s do not match the files — this backup is damaged"
say "sha256: los cuatro ficheros coinciden con el manifiesto"

# -- --check: prove it restores, against throwaway targets ------------------

if [[ $CHECK -eq 1 ]]; then
  PG="$(container postgres)"
  SCRATCH_DB="brain_restore_check_$$"
  SCRATCH_COLL="restore_check_$$"
  TMP="$(mktemp -d)"
  # Every scratch target is torn down whichever way this exits. Leaving a
  # collection behind would be the exact mistake CLAUDE.md records: leftover
  # test points once outnumbered real ones 105 to 5.
  cleanup() {
    docker exec "$PG" psql -U "$PG_USER" -d postgres -qc "drop database if exists ${SCRATCH_DB};" >/dev/null 2>&1 || true
    curl -sf -m 60 -X DELETE "http://127.0.0.1:${QDRANT_PORT}/collections/${SCRATCH_COLL}" >/dev/null 2>&1 || true
    rm -rf -- "$TMP"
  }
  trap cleanup EXIT

  say ""
  say "== el catálogo se replica en una base desechable =="
  docker exec "$PG" psql -U "$PG_USER" -d postgres -qc "create database ${SCRATCH_DB};" >/dev/null
  gzip -dc "${FROM}/catalog.sql.gz" \
    | docker exec -i "$PG" psql -U "$PG_USER" -d "$SCRATCH_DB" -q -v ON_ERROR_STOP=1 >/dev/null \
    || die "the catalog dump does not replay"
  docker exec "$PG" psql -U "$PG_USER" -d "$SCRATCH_DB" -tAc "
    select '  ' || table_name || ': ' || (xpath('/row/c/text()',
      query_to_xml('select count(*) as c from ' || quote_ident(table_name), false, true, '')))[1]::text
    from information_schema.tables where table_schema='public' order by table_name;"

  say ""
  say "== el snapshot de Qdrant se carga en una colección desechable =="
  curl -sf -m 900 -X POST \
    "http://127.0.0.1:${QDRANT_PORT}/collections/${SCRATCH_COLL}/snapshots/upload?priority=snapshot" \
    -H 'Content-Type:multipart/form-data' \
    -F "snapshot=@${FROM}/qdrant-${COLLECTION}.snapshot" >/dev/null \
    || die "Qdrant refused the snapshot"
  curl -sf -m 60 "http://127.0.0.1:${QDRANT_PORT}/collections/${SCRATCH_COLL}" \
    | python3 -c 'import json,sys
r = json.load(sys.stdin)["result"]
print("  points:", r.get("points_count"), " status:", r.get("status"))'

  say ""
  say "== el tar del workspace se extrae =="
  tar -xzf "${FROM}/workspace.tar.gz" -C "$TMP"
  printf '  %s chunks.jsonl, %s semantics.json, %s perfiles\n' \
    "$(find "$TMP" -name chunks.jsonl | wc -l)" \
    "$(find "$TMP" -name 'semantics*.json' | wc -l)" \
    "$(find "$TMP/profiles" -name '*.json' 2>/dev/null | wc -l)"

  say ""
  say "== el snapshot de Memgraph está presente y no está vacío =="
  # Not loaded: Memgraph community has one database, so proving this leg would
  # mean overwriting the live graph. Size and header are all that is checked.
  [[ -s "${FROM}/memgraph.snapshot" ]] || die "memgraph.snapshot is empty"
  printf '  %s bytes (no cargado — ver --memgraph)\n' "$(stat -c %s "${FROM}/memgraph.snapshot")"

  say ""
  say "== restaurable =="
  exit 0
fi

# -- the destructive legs ---------------------------------------------------

[[ $((DO_CATALOG + DO_WORKSPACE + DO_QDRANT + DO_MEMGRAPH)) -gt 0 ]] \
  || die "nothing to do: pass --check, or one of --catalog/--workspace/--qdrant/--memgraph/--all"
[[ $YES -eq 1 ]] || die "this replaces live data. Re-run with --yes once you mean it."

say ""
say "== restaurando desde ${FROM} =="

if [[ $DO_CATALOG -eq 1 ]]; then
  say "-- catálogo"
  PG="$(container postgres)"
  # Dropped and recreated rather than restored over: a dump replayed onto an
  # existing schema leaves rows the backup never had, and the catalog is the
  # thing everything else is checked against.
  docker exec "$PG" psql -U "$PG_USER" -d postgres -qc \
    "select pg_terminate_backend(pid) from pg_stat_activity where datname='brain' and pid <> pg_backend_pid();" >/dev/null
  docker exec "$PG" psql -U "$PG_USER" -d postgres -qc "drop database if exists brain;" >/dev/null
  docker exec "$PG" psql -U "$PG_USER" -d postgres -qc "create database brain;" >/dev/null
  gzip -dc "${FROM}/catalog.sql.gz" \
    | docker exec -i "$PG" psql -U "$PG_USER" -d brain -q -v ON_ERROR_STOP=1 >/dev/null
  say "   ok"
fi

if [[ $DO_WORKSPACE -eq 1 ]]; then
  say "-- artefactos del workspace (aditivo)"
  tar -xzf "${FROM}/workspace.tar.gz" -C "$WORKSPACE"
  say "   ok"
fi

if [[ $DO_QDRANT -eq 1 ]]; then
  say "-- Qdrant"
  curl -sf -m 120 -X DELETE "http://127.0.0.1:${QDRANT_PORT}/collections/${COLLECTION}" >/dev/null || true
  curl -sf -m 900 -X POST \
    "http://127.0.0.1:${QDRANT_PORT}/collections/${COLLECTION}/snapshots/upload?priority=snapshot" \
    -H 'Content-Type:multipart/form-data' \
    -F "snapshot=@${FROM}/qdrant-${COLLECTION}.snapshot" >/dev/null \
    || die "Qdrant refused the snapshot"
  say "   ok"
fi

if [[ $DO_MEMGRAPH -eq 1 ]]; then
  say "-- Memgraph (esta pata no ha sido ejercitada contra una pila viva)"
  MG="$(container memgraph)"
  # Memgraph recovers from the newest snapshot *plus* the write-ahead log, so
  # the log has to go with it — left in place it would replay the very
  # transactions the restore is undoing.
  docker exec "$MG" sh -c 'rm -f /var/lib/memgraph/wal/* || true'
  docker exec "$MG" sh -c 'rm -f /var/lib/memgraph/snapshots/* || true'
  docker cp "${FROM}/memgraph.snapshot" "${MG}:/var/lib/memgraph/snapshots/restored"
  docker exec "$MG" sh -c 'chown memgraph:memgraph /var/lib/memgraph/snapshots/restored'
  say "   copiado. Reinicia el contenedor para que lo cargue:"
  say "   docker restart ${MG}"
fi

say ""
say "== hecho =="
say "Las proyecciones y el catálogo se tomaron segundos aparte, así que pueden"
say "diferir en un documento indexado durante el backup. El catálogo manda:"
say "un Rebuild del documento afectado reconvergen las dos proyecciones."
