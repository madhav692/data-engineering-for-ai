-- A4, "The Request Log": five queries against Stage 0's requests table, plus the eval join.
-- Run them all:   make sql FILE=docs/sql/a4-request-log.sql
-- Or one at a time from `make psql`. Each query is self-contained: Q1 and Q5 pick the most
-- recent request / its top chunk; pin an id instead where the comment says so.

SET TIME ZONE 'UTC';

\echo
\echo '=== Q1  Reproduce: what the model saw, and is the text still the text it saw ==='
WITH r AS (
  SELECT * FROM requests WHERE status = 'ok' ORDER BY received_at DESC LIMIT 1
  -- or: SELECT * FROM requests WHERE request_id = 'req_...'
)
SELECT (rc->>'rank')::int AS rank, c.chunk_id, d.uri, c.token_count,
       encode(sha256(convert_to(c.text, 'UTF8')), 'hex') = rc->>'text_sha256' AS text_intact
FROM r
CROSS JOIN LATERAL jsonb_array_elements(r.retrieved_chunks) rc
JOIN chunks c           ON c.chunk_id = rc->>'chunk_id'
JOIN latest_documents d ON d.doc_id = c.doc_id
ORDER BY rank;

\echo
\echo '=== Q2  Debug: which versions served traffic, and when (the ledger derived from traffic) ==='
SELECT prompt_template_version AS prompt, retrieval_config_version AS retrieval,
       embedding_model_id AS embedding, llm_id,
       left(index_snapshot_id, 16) AS snapshot, count(*) AS requests,
       to_char(min(received_at), 'MM-DD HH24:MI') AS first_seen,
       to_char(max(received_at), 'MM-DD HH24:MI') AS last_seen
FROM requests
GROUP BY 1, 2, 3, 4, 5
ORDER BY min(received_at);

\echo
\echo '=== Q3  Cost and latency by generator ==='
SELECT llm_id, count(*) AS n,
       round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY (latency->>'generate_ms')::int)::numeric, 1) AS gen_p50_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY (latency->>'total_ms')::int)::numeric, 1)    AS total_p95_ms,
       sum(tokens_in + tokens_out) AS tokens, round(sum(cost_usd)::numeric, 4) AS usd,
       count(*) FILTER (WHERE status <> 'ok') AS failed
FROM requests
GROUP BY llm_id
ORDER BY n DESC;

\echo
\echo '=== Q4  Evaluate with feedback: thumbs down per version (C7 dedup keeps the count honest) ==='
SELECT r.llm_id, r.prompt_template_version AS prompt,
       count(DISTINCT r.request_id) AS answered,
       count(f.feedback_id) FILTER (WHERE f.kind = 'thumbs' AND f.value < 0) AS thumbs_down,
       count(f.feedback_id) FILTER (WHERE f.kind = 'thumbs' AND f.value > 0) AS thumbs_up
FROM requests r
LEFT JOIN feedback f ON f.request_id = r.request_id
GROUP BY 1, 2
ORDER BY thumbs_down DESC, answered DESC;

\echo
\echo '=== Q4b Offline is the same table: every eval run beside the requests it made ==='
SELECT left(e.run_id, 20) AS run, to_char(e.started_at, 'MM-DD HH24:MI:SS') AS started,
       e.config->>'llm_id' AS llm_id, left(e.config->>'index_snapshot_id', 16) AS snapshot,
       round((e.metrics->>'recall_at_k')::numeric, 2) AS recall_at_5,
       round((e.metrics->>'mrr')::numeric, 2) AS mrr,
       (SELECT count(*) FROM requests r
         WHERE r.received_at BETWEEN e.started_at AND e.finished_at) AS requests_made
FROM eval_runs e
ORDER BY e.started_at DESC;

\echo
\echo '=== Q5  Blast radius: every request that put one chunk in front of a model ==='
WITH top AS (
  SELECT rc->>'chunk_id' AS chunk_id
  FROM requests r CROSS JOIN LATERAL jsonb_array_elements(r.retrieved_chunks) rc
  WHERE r.status = 'ok' AND (rc->>'rank')::int = 1
  ORDER BY r.received_at DESC LIMIT 1
  -- or: SELECT 'chk_...' AS chunk_id
)
SELECT r.request_id, to_char(r.received_at, 'MM-DD HH24:MI:SS') AS received,
       (rc->>'rank')::int AS rank, r.status, left(r.query_text, 48) AS question
FROM requests r
CROSS JOIN LATERAL jsonb_array_elements(r.retrieved_chunks) rc
JOIN top ON rc->>'chunk_id' = top.chunk_id
ORDER BY r.received_at DESC
LIMIT 8;
-- The containment form, served by a GIN index: WHERE retrieved_chunks @> '[{"chunk_id": "chk_..."}]'
