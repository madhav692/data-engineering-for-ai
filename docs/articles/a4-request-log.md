# A4 — The Request Log: The Most Important Table You Are Not Building

Written from `stage-0`. The queries and the two drills run against a Stage 0 that has answered a
few dozen requests (`make ask`, `make eval`).

| Section | Where it lives |
| --- | --- |
| Anatomy of a request row | `packages/schemas/src/cairn_schemas/models.py` (`Request`, `RetrievedChunk`, `LlmParams`, `StageLatency`, the `Costed` and `Row` mixins); the generated DDL `packages/schemas/generated/postgres/requests.sql` |
| Write it before you answer | the `/ask` handler in `platform/src/cairn/serving/app.py`; contract C6 in `docs/architecture/reference.md`; `test_ask_writes_request_before_responding` in `platform/tests/integration/test_c6_c7_serving.py` |
| Five queries that pay for the table | `docs/sql/a4-request-log.sql`, run with `make sql FILE=docs/sql/a4-request-log.sql`; the eval join rests on contract C8 and `test_eval_run_config_matches_request_keys` |
| What stays out, what gets hashed | the `Request` docstring and field descriptions; `PARTITION_BY = ("days(received_at)",)` |
| Hot table, cold history, one writer | `platform/src/cairn/lineage/request_log.py` (`RequestLog`); the indexes in `platform/src/cairn/lakehouse/migrations/0001_platform.sql` (`requests_by_received_at`, `requests_by_chunk`); `cairn_schemas.ids.new_request_id` (UUIDv7) |
| Run it | `make requests`, `make replay`, `make feedback`, `make eval`, `make sql`; `cairn replay` exits 1 when a logged chunk fails its hash check or the re-run disagrees (`platform/src/cairn/cli.py`) |
| Figures | `docs/architecture/diagrams/a4-fig2-write-path.mmd`, `a4-fig3-join-graph.mmd`; Fig 1 is a hand-drawn SVG |
