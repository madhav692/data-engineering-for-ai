#!/usr/bin/env bash
# Measure Cairn Stage 0 on this machine and write one log for platform/CHANGES-stage-0.md.
#
# Standalone: run it from any empty directory. It clones the repository, runs the reader's sequence
# (make start, ingest, build-index, ask) with a wall clock on every step, then collects every
# number the "Measured numbers" table asks for: image sizes, the model cache, memory at rest and
# during ingest, the golden set, retrieval latency, corpus and database statistics, both test suites.
#
#   bash measure-stage-0.sh --cold --yes      # the numbers the article quotes: no images, no cache
#   bash measure-stage-0.sh --yes             # warm: keep images and build cache, wipe only Cairn's data
#
# --cold removes the three Cairn images and ALL of Docker's build cache (other projects' too), then
# pulls and builds again. Both modes remove Cairn's containers and volumes (database, objects, model
# cache): anything ingested so far is gone. --yes skips the confirmation.
#
# Environment: REPO_URL (default: the series repository), REF (default: main), CAIRN_PG_PORT and
# the other CAIRN_*_PORT variables are passed through to compose.
#
# Output: ./stage-0-measure-<timestamp>/measurements.log (send this one) and raw/<step>.log with the
# complete output of every step. Works with macOS's bash 3.2 and with Linux.

set -u

REPO_URL="${REPO_URL:-https://github.com/madhav692/data-engineering-for-ai.git}"
REF="${REF:-main}"
QUESTION='How does Iceberg hidden partitioning work?'
IMAGES="pgvector/pgvector:pg16 chrislusf/seaweedfs:4.48 cairn-api:stage-0"

COLD=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --cold) COLD=1 ;;
    --yes) YES=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$PWD/stage-0-measure-$STAMP"
RAW="$WORK/raw"
LOG="$WORK/measurements.log"
CLONE="$WORK/data-engineering-for-ai"
mkdir -p "$RAW"

# ---- helpers -------------------------------------------------------------------------------------

log() { printf '%s\n' "$*" | tee -a "$LOG"; }
section() { log ""; log "=== $* ==="; }
now() { date +%s; }

# run_step NAME [--quiet] CMD...: run CMD, keep its full output in raw/NAME.log, copy it to the
# main log (minus the embedding progress lines), and record exit status and wall-clock seconds.
run_step() {
  local name="$1"; shift
  local quiet=0
  if [ "${1:-}" = "--quiet" ]; then quiet=1; shift; fi
  section "$name: $*"
  local t0 t1 status
  t0=$(now)
  "$@" > "$RAW/$name.log" 2>&1
  status=$?
  t1=$(now)
  if [ "$quiet" -eq 0 ]; then
    grep -v '^  embedding [0-9]*/[0-9]*$' "$RAW/$name.log" | tee -a "$LOG"
  else
    tail -n 12 "$RAW/$name.log" | tee -a "$LOG"
  fi
  log "--- $name: exit $status, $((t1 - t0)) s wall clock"
  eval "T_$(printf '%s' "$name" | tr -c 'A-Za-z0-9_\n' '_')=$((t1 - t0))"
  return $status
}

docker_mem() {  # one line per container: name, memory, cpu
  docker stats --no-stream --format '{{.Name}}  mem {{.MemUsage}}  cpu {{.CPUPerc}}' 2>/dev/null \
    | grep -E '^cairn-' || echo "(no cairn containers running)"
}

# ---- confirmation --------------------------------------------------------------------------------

echo "This will remove Cairn's containers and volumes (database, raw objects, model cache)."
if [ "$COLD" -eq 1 ]; then
  echo "COLD: it will also remove the images ($IMAGES) and ALL of Docker's build cache."
fi
echo "Work directory: $WORK"
if [ "$YES" -ne 1 ]; then
  printf 'Continue? [y/N] '
  read -r answer
  case "$answer" in y|Y|yes) ;; *) echo "aborted"; exit 1 ;; esac
fi

# ---- 0. the machine ------------------------------------------------------------------------------

section "machine"
log "date            $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC), local $(date)"
log "mode            $([ "$COLD" -eq 1 ] && echo cold || echo warm)"
log "repo            $REPO_URL @ $REF"
if command -v sw_vers >/dev/null 2>&1; then
  log "os              $(sw_vers -productName) $(sw_vers -productVersion) ($(uname -m))"
  log "cpu             $(sysctl -n machdep.cpu.brand_string 2>/dev/null) · $(sysctl -n hw.ncpu) cores"
  log "memory          $(( $(sysctl -n hw.memsize) / 1073741824 )) GB"
else
  log "os              $(uname -srm)"
  log "cpu             $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ //') · $(nproc 2>/dev/null) cores"
  log "memory          $(awk '/MemTotal/ {printf "%d GB", $2/1048576}' /proc/meminfo 2>/dev/null)"
fi
log "docker          $(docker version --format 'client {{.Client.Version}}, server {{.Server.Version}}' 2>/dev/null)"
log "docker vm       $(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} B memory, {{.OperatingSystem}}, {{.Architecture}}' 2>/dev/null)"
log "compose         $(docker compose version --short 2>/dev/null)"
log "uv              $(uv --version 2>/dev/null || echo 'not installed (make test-platform will be skipped)')"
log "git             $(git --version)"
log "make            $(make --version 2>/dev/null | head -1)"

# ---- 1. a clean slate ----------------------------------------------------------------------------

section "clean slate"
for c in cairn-api cairn-postgres cairn-seaweedfs; do
  docker rm -f "$c" >/dev/null 2>&1 && log "removed container $c" || log "container $c not present"
done
for v in cairn_pgdata cairn_objects cairn_model-cache; do
  docker volume rm "$v" >/dev/null 2>&1 && log "removed volume $v" || log "volume $v not present"
done
docker network rm cairn_default >/dev/null 2>&1 || true
if [ "$COLD" -eq 1 ]; then
  for image in $IMAGES; do docker rmi "$image" >/dev/null 2>&1 && log "removed image $image" || log "image $image not present"; done
  docker builder prune -af >/dev/null 2>&1 && log "pruned the build cache" || log "could not prune the build cache"
fi
log "docker system df:"
docker system df 2>/dev/null | tee -a "$LOG"

# ---- 2. the reader's sequence, timed -------------------------------------------------------------

T_TOTAL0=$(now)
cd "$WORK"
run_step clone git clone --quiet --branch "$REF" "$REPO_URL" "$CLONE" || { log "clone failed; stopping"; exit 1; }
cd "$CLONE"
log "commit          $(git rev-parse HEAD) ($(git describe --tags --always 2>/dev/null))"

run_step start --quiet make start || { log "make start failed; stopping (full output in raw/start.log)"; exit 1; }
section "memory at rest (after make start)"
docker_mem | tee -a "$LOG"
section "docker vm disk (seen by seaweedfs)"
docker exec cairn-seaweedfs df -h /data 2>/dev/null | tee -a "$LOG"

# ingest in the background; sample memory every 10 s while it runs
section "ingest: make ingest (memory sampled every 10 s)"
T_INGEST0=$(now)
make ingest > "$RAW/ingest.log" 2>&1 &
INGEST_PID=$!
while kill -0 "$INGEST_PID" 2>/dev/null; do
  sleep 10
  kill -0 "$INGEST_PID" 2>/dev/null || break
  log "[+$(( $(now) - T_INGEST0 )) s] $(docker_mem | tr '\n' ';')"
done
wait "$INGEST_PID"; INGEST_STATUS=$?
T_INGEST1=$(now)
grep -v '^  embedding [0-9]*/[0-9]*$' "$RAW/ingest.log" | tee -a "$LOG"
log "--- ingest: exit $INGEST_STATUS, $((T_INGEST1 - T_INGEST0)) s wall clock"
T_ingest=$((T_INGEST1 - T_INGEST0))

run_step build-index make build-index
run_step ask make ask Q="$QUESTION"
T_TOTAL1=$(now)
T_SUM=$(( ${T_clone:-0} + ${T_start:-0} + ${T_ingest:-0} + ${T_build_index:-0} + ${T_ask:-0} ))
section "clone to first answer"
log "clone ${T_clone:-?} s + start ${T_start:-?} s + ingest ${T_ingest:-?} s + build-index ${T_build_index:-?} s + ask ${T_ask:-?} s = $T_SUM s (the table's number; $((T_TOTAL1 - T_TOTAL0)) s wall clock including the memory snapshots)"

# ---- 3. everything else --------------------------------------------------------------------------

section "images"
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}  (created {{.CreatedSince}})' | grep -E 'pgvector|seaweedfs|cairn-api' | tee -a "$LOG"
section "model cache"
docker exec cairn-api du -sh /models 2>/dev/null | tee -a "$LOG"
docker exec cairn-api sh -c 'find /models -type f -size +1M -exec ls -l {} \;' 2>/dev/null | awk '{print $5, $9}' | tee -a "$LOG"

run_step requests make requests N=3
FIRST_REQ=$(grep -o 'req_[0-9a-f]*' "$RAW/ask.log" | head -1)
if [ -n "$FIRST_REQ" ]; then run_step replay make replay REQ="$FIRST_REQ"; else log "no request_id found in the ask output"; fi
run_step lag make lag
run_step eval make eval                     # 20 more requests: 21 in total
run_step metrics --quiet make metrics
section "retrieval latency over 21 requests (from /metrics)"
grep -E 'cairn_request_latency_ms|cairn_requests_total|cairn_cost_usd_total|cairn_index_lag_seconds' "$RAW/metrics.log" | tee -a "$LOG"
run_step stats make stats
section "memory idle (after eval)"
docker_mem | tee -a "$LOG"

run_step test make test
section "test: pytest summary"
grep -E '[0-9]+ passed|[0-9]+ failed|error' "$RAW/test.log" | tail -2 | tee -a "$LOG"

if command -v uv >/dev/null 2>&1; then
  run_step setup --quiet make setup           # the host toolchain; not a table row
  run_step test-platform make test-platform
  section "test-platform: pytest summary"
  grep -E '[0-9]+ passed|[0-9]+ failed|[0-9]+ skipped|error' "$RAW/test-platform.log" | tail -2 | tee -a "$LOG"
else
  section "test-platform: skipped (uv not installed)"
fi

# ---- 4. where everything landed -------------------------------------------------------------------

section "summary"
log "clone to first answer   $T_SUM s  (clone ${T_clone:-?} · start ${T_start:-?} · ingest ${T_ingest:-?} · build-index ${T_build_index:-?} · ask ${T_ask:-?})"
log "make test               ${T_test:-?} s wall clock (pytest's own time is above)"
log "make test-platform      ${T_test_platform:-skipped} s wall clock (pytest's own time is above)"
log "log                     $LOG"
log "raw output              $RAW/"
echo
echo "Done. Send measurements.log (and, if asked, the raw/ folder) to fill in CHANGES-stage-0.md."
echo "The stack is still running in $CLONE: make down there when you are finished with it."
