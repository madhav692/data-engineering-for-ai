"""Contracts C6 and C7, and the thesis. Serving writes the request row before it answers; replay
reproduces what the model saw; feedback is idempotent; swapping the model changes only the
generator's fields."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cairn_schemas import ids
from cairn_schemas.models import Request

from cairn.lineage import replay as replay_mod
from support import Platform

QUESTION = "How does hidden partitioning work?"


def test_ask_writes_request_before_responding(indexed: Platform):
    """The request_id in the HTTP response already has a row with every lineage field."""
    reply = indexed.ask(QUESTION)
    row = indexed.request(reply["request_id"])  # the row is there when the client reads the reply

    assert row.status == "ok"
    for name in Request.LINEAGE_FIELDS:
        assert hasattr(row, name)
    assert row.lineage() == {
        "prompt_template_version": "answer-v1",
        "retrieval_config_version": "vector-k5-v1",
        "index_snapshot_id": reply["index_snapshot_id"],
        "embedding_model_id": indexed.embedder.model_id,
        "reranker_id": None,
        "llm_id": "cairn/extractive@0",
    }
    assert row.query_sha256 == ids.sha256_hex(QUESTION)
    assert row.response_sha256 == ids.sha256_hex(reply["answer"])
    assert [c.rank for c in row.retrieved_chunks] == list(range(1, len(row.retrieved_chunks) + 1))
    assert 1 <= len(row.retrieved_chunks) <= 5
    assert [c["chunk_id"] for c in reply["citations"]] == [c.chunk_id for c in row.retrieved_chunks]
    assert row.filters == {"tenant_id": "local"}
    assert row.latency.total_ms >= row.latency.retrieve_ms


def test_replay_reproduces_retrieved_chunk_ids(indexed: Platform):
    """Same snapshot, same configuration, same chunk ids, same order; same answer bytes."""
    reply = indexed.ask(QUESTION)
    indexed.ask("Unrelated question about refunds")  # other traffic changes nothing for replay

    report = replay_mod.replay(indexed.db, indexed.embedder, reply["request_id"])
    assert report.chunk_ids_match is True
    assert report.same_order is True
    assert report.response_matches is True
    assert report.inconsistent_chunks == []
    text = replay_mod.format_report(report)
    assert "5 of 5 chunk ids match, same order" in text or "chunk ids match, same order" in text
    assert "response sha256 matches" in text

    # a corrupted row refuses to load: the validator from A2, not a check written here
    victim = report.request.retrieved_chunks[0].chunk_id
    indexed.db.execute("UPDATE chunks SET text = text || 'x' WHERE chunk_id = %s", (victim,))
    damaged = replay_mod.replay(indexed.db, indexed.embedder, reply["request_id"])
    assert damaged.inconsistent_chunks == [victim]
    assert "inconsistent" in replay_mod.format_report(damaged)


def test_duplicate_feedback_counts_once(indexed: Platform):
    """The same dedup_key twice makes one row; a signal for an unknown request is rejected."""
    reply = indexed.ask(QUESTION)
    body = {
        "request_id": reply["request_id"],
        "kind": "thumbs",
        "value": -1,
        "dedup_key": "ui-7f3a",
    }

    first = indexed.client.post("/feedback", json=body)
    second = indexed.client.post("/feedback", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json() == {"feedback_id": first.json()["feedback_id"], "duplicate": True}
    assert indexed.count("feedback") == 1

    unknown = indexed.client.post("/feedback", json={**body, "request_id": ids.new_request_id()})
    assert unknown.status_code == 404
    assert indexed.count("feedback") == 1


class _FakeOpenAI(BaseHTTPRequestHandler):
    """A /v1/chat/completions endpoint that answers every prompt the same way."""

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length))
        reply = {
            "id": "chatcmpl-test",
            "model": request["model"] + "-2026-01-01",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hidden partitioning derives partition values from columns [1].",
                    },
                }
            ],
            "usage": {"prompt_tokens": 321, "completion_tokens": 12},
        }
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def fake_llm():
    server = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


def test_generator_swap_changes_only_llm_fields(indexed: Platform, fake_llm: str):
    """Against a fake OpenAI-compatible server, the only fields that differ between the two rows
    are the generator's: llm_id, llm_params, tokens, cost, the response hash and generate_ms."""
    default = indexed.request(indexed.ask(QUESTION)["request_id"])
    swapped = indexed.request(
        indexed.ask(
            QUESTION,
            generator={"base_url": fake_llm, "model": "test-model", "provider": "fake"},
        )["request_id"]
    )

    # everything upstream of the generator is identical
    for name in (
        "prompt_template_version",
        "retrieval_config_version",
        "index_snapshot_id",
        "embedding_model_id",
        "reranker_id",
        "filters",
        "query_sha256",
    ):
        assert getattr(default, name) == getattr(swapped, name), name
    assert default.retrieved_chunks == swapped.retrieved_chunks

    # and the generator's fields are the only ones that moved
    assert default.llm_id == "cairn/extractive@0"
    assert swapped.llm_id == "fake/test-model@test-model-2026-01-01"
    assert (swapped.tokens_in, swapped.tokens_out) == (321, 12)
    assert swapped.cost_usd == 0.0  # unknown model: unpriced, recorded as 0
    assert swapped.response_sha256 != default.response_sha256
    _, changed = replay_mod.diff(indexed.db, default.request_id, swapped.request_id)
    assert set(changed) <= {
        "llm_id",
        "llm_params",
        "tokens",
        "cost_usd",
        "response_sha256",
        "latency.generate_ms",
    }
    assert {"llm_id", "llm_params", "tokens", "response_sha256"} <= set(changed)
