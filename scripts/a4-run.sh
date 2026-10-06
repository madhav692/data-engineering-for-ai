#!/usr/bin/env bash
# Run everything A4 ("The Request Log") shows, against the Stage 0 that is already running, and
# write one log to paste from: the article's request and its row, `make requests`, `make replay`,
# the duplicate-feedback check, two evaluation runs, the failed-generator drill, the tamper drill,
# the five queries from docs/sql/a4-request-log.sql, and the row-size and insert-latency numbers.
#
#   bash scripts/a4-run.sh            # from the repository root; Stage 0 up, ingested, indexed
#
# It adds rows (one ask, two eval runs, a few feedback rows, one failed request) and touches
# nothing else: the tamper drill restores the row it edits, the insert timing deletes its copies.
# Optional: with CAIRN_LLM_BASE_URL / CAIRN_LLM_MODEL / CAIRN_LLM_PROVIDER set in the environment
# (the A3 swap, e.g. Ollama on the host), it also asks three questions through that generator so
# query 3 has a second llm_id with tokens. Works with macOS's bash 3.2 and with Linux.

set -u

QUESTION='How does Iceberg hidden partitioning work?'
DRILL_QUESTION='Why do vector indexes go stale?'
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$PWD/a4-run-$STAMP"
RAW="$WORK/raw"
LOG="$WORK/a4-run.log"
mkdir -p "$RAW"

COMPOSE="docker compose --project-directory . -f infra/compose/base.yml --profile core"
PSQL() { $COMPOSE exec -T postgres psql -U cairn -d cairn -X -q "$@"; }

log() { printf '%s\n' "$*" | tee -a "$LOG"; }
section() { log ""; log "=== $* ==="; }
filter() { grep -v -E 'huggingface_hub/utils/tqdm.py|warnings.warn\(|^docker compose '; }

# run_step NAME CMD...: full output in raw/NAME.log, the interesting lines in the main log, exit status.
run_step() {
  local name="$1"; shift
  section "$name: $*"
  "$@" > "$RAW/$name.log" 2>&1
  local status=$?
  filter < "$RAW/$name.log" | tee -a "$LOG"
  log "--- $name: exit $status"
  return $status
}

# ---- 0. preconditions ------------------------------------------------------------------------------

if [ ! -f docs/sql/a4-request-log.sql ] || ! grep -q '^sql:' Makefile; then
  echo "run this from the repository root, on main after the A4 follow-up PR (docs/sql/a4-request-log.sql, make sql)" >&2
  exit 2
fi
if ! curl -sf "http://localhost:${CAIRN_API_PORT:-8000}/healthz" > "$RAW/healthz.json" 2>/dev/null; then
  echo "the api is not answering on :${CAIRN_API_PORT:-8000}; make start first (and make ingest && make build-index)" >&2
  exit 2
fi

section "run"
log "date            $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC), local $(date)"
log "commit          $(git rev-parse --short HEAD 2>/dev/null) ($(git describe --tags --always 2>/dev/null)), branch $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
log "healthz         $(head -c 400 "$RAW/healthz.json")"
log "swap generator  ${CAIRN_LLM_BASE_URL:-not set (query 3 will show the extractive generator only)}"
run_step stats-before make stats

# ---- 1. the article's request ------------------------------------------------------------------------

# The article's request goes through the default generator whatever the shell exports; the swap
# (CAIRN_LLM_*) is used only for the three swap questions below.
NOSWAP="env -u CAIRN_LLM_BASE_URL -u CAIRN_LLM_MODEL -u CAIRN_LLM_PROVIDER -u CAIRN_LLM_API_KEY"
run_step ask $NOSWAP make ask Q="$QUESTION"
REQ=$(grep -o 'req_[0-9a-f]*' "$RAW/ask.log" | head -1)
if [ -z "$REQ" ]; then log "no request_id in the ask output; stopping"; exit 1; fi
log "REQ             $REQ"

section "the row: SELECT * FROM requests WHERE request_id = '$REQ' (expanded, first retrieved chunk pretty-printed)"
PSQL -c "SET TIME ZONE 'UTC';" -x -c "SELECT request_id, tenant_id, received_at, status, query_sha256, prompt_template_version, retrieval_config_version, index_snapshot_id, embedding_model_id, reranker_id, llm_id, llm_params, filters, response_sha256, latency, tokens_in, tokens_out, cost_usd, trace_id, schema_version, length(response_text) AS response_chars FROM requests WHERE request_id = '$REQ';" 2>&1 | tee -a "$LOG"
PSQL -c "SELECT jsonb_pretty(retrieved_chunks->0) FROM requests WHERE request_id = '$REQ';" 2>&1 | tee -a "$LOG"
section "the five chunks of that row (for Fig 1)"
PSQL -c "SELECT (rc->>'rank')::int AS rank, left(rc->>'chunk_id', 20) AS chunk_id, left(rc->>'text_sha256', 16) AS text_sha256, round((rc->>'score')::numeric, 4) AS score FROM requests r, jsonb_array_elements(r.retrieved_chunks) rc WHERE r.request_id = '$REQ' ORDER BY 1;" 2>&1 | tee -a "$LOG"

run_step requests make requests N=3
run_step replay make replay REQ="$REQ"
log "--- replay exit code above should be 0"

# ---- 2. feedback, evaluation, the optional swap -----------------------------------------------------------

run_step feedback-1 make feedback REQ="$REQ" KIND=thumbs VALUE=-1 KEY="a4-demo-$STAMP"
run_step feedback-2 make feedback REQ="$REQ" KIND=thumbs VALUE=-1 KEY="a4-demo-$STAMP"
log "--- the second feedback must say duplicate: no new row"

run_step eval-1 make eval
run_step eval-2 make eval

section "a few more feedback rows, so query 4 has something to count"
IDS=$($COMPOSE exec -T api cairn requests --limit 14 2>/dev/null | awk '$4 == "ok" {print $1}')
n=0
for id in $IDS; do
  n=$((n + 1))
  if [ "$n" -le 2 ]; then
    make feedback REQ="$id" KIND=thumbs VALUE=-1 COMMENT="cites the wrong section" 2>&1 | filter | grep -E 'feedback' | tee -a "$LOG"
  elif [ "$n" -le 7 ]; then
    make feedback REQ="$id" KIND=thumbs VALUE=1 2>&1 | filter | grep -E 'feedback' | tee -a "$LOG"
  fi
done

if [ -n "${CAIRN_LLM_BASE_URL:-}" ]; then
  run_step swap-1 make ask Q="$QUESTION"
  if grep -q 'api returned 502' "$RAW/swap-1.log"; then
    log "!! the swap endpoint $CAIRN_LLM_BASE_URL did not answer (see above). With Ollama: ollama list, then"
    log "!! ollama pull ${CAIRN_LLM_MODEL:-<model>}; or unset CAIRN_LLM_BASE_URL and run again. Skipping the other two."
  else
    run_step swap-2 make ask Q="What is a manifest list and what does it store?"
    run_step swap-3 make ask Q="How do I expire old snapshots and why should I?"
  fi
fi

# ---- 3. drill 1: the answer fails, the row does not ----------------------------------------------------------

section "drill 1: CAIRN_LLM_BASE_URL=http://127.0.0.1:9 CAIRN_LLM_MODEL=none make ask Q=\"$DRILL_QUESTION\""
$NOSWAP CAIRN_LLM_BASE_URL=http://127.0.0.1:9 CAIRN_LLM_MODEL=none CAIRN_LLM_PROVIDER=openai-compatible \
  make ask Q="$DRILL_QUESTION" > "$RAW/drill-1-ask.log" 2>&1
log "--- make ask: exit $?"
filter < "$RAW/drill-1-ask.log" | tee -a "$LOG"
ERR_REQ=$(grep -o 'req_[0-9a-f]*' "$RAW/drill-1-ask.log" | head -1)
log "ERR_REQ         ${ERR_REQ:-not found}"
run_step drill-1-requests make requests N=1
if [ -n "$ERR_REQ" ]; then run_step drill-1-replay make replay REQ="$ERR_REQ"; fi

# ---- 4. drill 2: a tampered row cannot pass ---------------------------------------------------------------------

section "drill 2: tamper the logged text_sha256 of rank 1, replay, restore"
ORIG=$(PSQL -At -c "SELECT retrieved_chunks->0->>'text_sha256' FROM requests WHERE request_id = '$REQ';" 2>/dev/null | tr -d '[:space:]')
log "original text_sha256 of rank 1: $ORIG"
ZEROS=$(printf '0%.0s' $(seq 1 64))
PSQL -c "UPDATE requests SET retrieved_chunks = jsonb_set(retrieved_chunks, '{0,text_sha256}', '\"$ZEROS\"') WHERE request_id = '$REQ';" 2>&1 | tee -a "$LOG"
make replay REQ="$REQ" > "$RAW/drill-2-replay.log" 2>&1
TAMPER_STATUS=$?
filter < "$RAW/drill-2-replay.log" | sed -n '/^context/,$p' | tee -a "$LOG"
log "--- make replay on the tampered row: make exit $TAMPER_STATUS (non-zero expected: cairn replay exits 1, which make reports as Error 1 and returns as 2)"
PSQL -c "UPDATE requests SET retrieved_chunks = jsonb_set(retrieved_chunks, '{0,text_sha256}', '\"$ORIG\"') WHERE request_id = '$REQ';" 2>&1 | tee -a "$LOG"
make replay REQ="$REQ" > "$RAW/drill-2-restored.log" 2>&1
log "--- make replay after restoring: exit $? (expected 0)"

# ---- 5. the five queries -------------------------------------------------------------------------------------------

run_step sql make sql FILE=docs/sql/a4-request-log.sql

# ---- 6. the numbers -----------------------------------------------------------------------------------------------

section "row size (pg_column_size), response length, table size"
PSQL -c "SELECT round(avg(pg_column_size(r.*))) AS avg_row_bytes, max(pg_column_size(r.*)) AS max_row_bytes, round(avg(length(response_text))) AS avg_response_chars, count(*) AS ok_rows FROM requests r WHERE status = 'ok';" 2>&1 | tee -a "$LOG"
PSQL -c "SELECT pg_size_pretty(pg_total_relation_size('requests')) AS requests_with_indexes, pg_size_pretty(pg_relation_size('requests')) AS heap, count(*) AS rows FROM requests;" 2>&1 | tee -a "$LOG"

section "insert latency: seven single-row inserts of a copy of $REQ (\\timing), then the copies are deleted"
for i in 1 2 3 4 5 6 7; do
  PSQL -c '\timing on' -c "INSERT INTO requests SELECT tenant_id, created_at, schema_version, 'req_00test' || lpad('$i', 24, '0'), user_id_hash, received_at, query_text, query_sha256, prompt_template_version, retrieval_config_version, index_snapshot_id, embedding_model_id, filters, retrieved_chunks, reranker_id, llm_id, llm_params, response_text, response_sha256, latency, status, trace_id, tokens_in, tokens_out, cost_usd FROM requests WHERE request_id = '$REQ';" 2>&1 | grep -E '^Time' | tee -a "$LOG"
done
PSQL -c "DELETE FROM requests WHERE request_id LIKE 'req_00test%';" 2>&1 | tee -a "$LOG"

section "indexes on requests, and the blast-radius query's plan"
PSQL -c "\\di requests*" 2>&1 | tee -a "$LOG"
TOP_CHUNK=$(PSQL -At -c "SELECT retrieved_chunks->0->>'chunk_id' FROM requests WHERE request_id = '$REQ';" 2>/dev/null | tr -d '[:space:]')
log "-- with the planner's default: on a table this small it may prefer a sequential scan"
PSQL -c "EXPLAIN (COSTS OFF) SELECT request_id FROM requests WHERE retrieved_chunks @> '[{\"chunk_id\": \"$TOP_CHUNK\"}]';" 2>&1 | tee -a "$LOG"
log "-- with SET enable_seqscan = off: the index the query will use once the table is large"
PSQL -c "SET enable_seqscan = off;" -c "EXPLAIN (COSTS OFF) SELECT request_id FROM requests WHERE retrieved_chunks @> '[{\"chunk_id\": \"$TOP_CHUNK\"}]';" 2>&1 | tee -a "$LOG"

run_step lag make lag
run_step stats-after make stats

# ---- 7. where everything landed -------------------------------------------------------------------------------------

section "summary"
log "article request   $REQ"
log "failed request    ${ERR_REQ:-not found}"
log "tamper drill      make replay exit $TAMPER_STATUS (non-zero = the row was refused, as the article says)"
log "log               $LOG"
log "raw output        $RAW/"
echo
echo "Done. Send a4-run.log to replace the sandbox outputs in A4 Draft 1."
