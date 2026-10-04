-- Non-contract objects the platform needs at Stage 0. The seven contract tables are created
-- from the generated DDL (packages/schemas/generated/postgres/*.sql) before this file runs.
-- Index tables are created per build by cairn.indexes.build, not here.

-- The current state of the registry: the highest revision of every document. Derivation,
-- the index build and the lag query all read this, never the documents table directly.
CREATE OR REPLACE VIEW latest_documents AS
SELECT DISTINCT ON (doc_id) *
FROM documents
ORDER BY doc_id, revision DESC;

-- Lookups the pipeline makes on every run.
CREATE INDEX IF NOT EXISTS chunks_by_doc ON chunks (doc_id, content_sha256);
CREATE INDEX IF NOT EXISTS chunks_by_version ON chunks (parser_version, chunker_version);
CREATE INDEX IF NOT EXISTS embeddings_by_model ON embeddings (embedding_model_id);
CREATE INDEX IF NOT EXISTS requests_by_received_at ON requests (received_at);
CREATE INDEX IF NOT EXISTS index_snapshots_live ON index_snapshots (index_name) WHERE status = 'live';

-- Contract C7: a feedback producer's dedup_key is unique per tenant, so a redelivery is a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS feedback_dedup ON feedback (tenant_id, dedup_key);
