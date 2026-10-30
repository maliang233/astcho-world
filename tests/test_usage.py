"""Billing invariants, provider counter semantics and passive capture compatibility."""

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from astcho_world.apps.notes import MANIFEST as NOTES
from astcho_world.dashboard import create_dashboard_app
from astcho_world.kernel import Kernel
from astcho_world.store import Store
from astcho_world.usage import TZ, UsageCollector, normalize_usage, provider_id
from astcho_world.usage_capture import UsageReporter

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/apps.json"
TOKEN = "synthetic-usage-test-token"


def rate(ledger, **kwargs):
    return ledger.add_rate(
        {
            "provider": "fixture/v1",
            "model": "test",
            "currency": "CNY",
            "input_price": "2",
            "cached_price": "0.2",
            "output_price": "6",
            **kwargs,
        }
    )


def test_provider_token_semantics_and_missing_are_not_zero():
    dsh = normalize_usage(
        {"inputTokens": 771, "outputTokens": 132, "cacheReadTokens": 24192, "totalTokens": 25095},
        dsh=True,
    )
    api = normalize_usage(
        {
            "prompt_tokens": 24963,
            "completion_tokens": 132,
            "prompt_tokens_details": {"cached_tokens": 24192},
            "completion_tokens_details": {"reasoning_tokens": 80},
        }
    )
    assert dsh == {**api, "reasoning": None}
    assert dsh["input"] + dsh["output"] == 25095
    assert normalize_usage({})["input"] is None
    assert normalize_usage({"prompt_tokens": float("inf")})["input"] is None
    assert normalize_usage({"prompt_tokens": 5, "prompt_cache_hit_tokens": 9})["cached"] is None
    assert (
        provider_id("https://user:secret@api.example:8443/v1?api_key=secret")
        == "api.example:8443/v1"
    )


def test_price_snapshot_cache_cost_zero_prices_and_no_retroactive_repricing(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    u = store.usage
    try:
        u.begin("unpriced", provider="fixture/v1", model="test")
        u.finish("unpriced", raw={"prompt_tokens": 1000, "completion_tokens": 100})
        version = rate(u)
        u.begin("priced", provider="fixture/v1", model="test")
        u.finish(
            "priced",
            raw={"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 800},
            status="failed",
        )
        u.finish("priced", status="failed")  # End report does not count/cache-charge twice.
        rate(u, input_price="200", output_price="600")
        u.begin("missing-cache", provider="fixture/v1", model="test")
        u.finish("missing-cache", raw={"prompt_tokens": 1000, "completion_tokens": 100})
        rate(u, input_price="0", cached_price="0", output_price="0")
        u.begin("free", provider="fixture/v1", model="test")
        u.finish("free", raw={"prompt_tokens": 1000, "completion_tokens": 100})
        rows = {r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")}
        assert rows["unpriced"]["cost_state"] == "unpriced"
        assert rows["priced"]["rate_id"] == version
        assert Decimal(rows["priced"]["cost"]) == Decimal("0.00116")
        assert rows["priced"]["status"] == "failed"
        assert rows["missing-cache"]["cost_state"] == "unknown_cache"
        assert rows["free"]["cost"] == "0"
        assert rows["free"]["cost_state"] == "estimated"
        assert u.report()["totals"]["calls"] == 4
    finally:
        store.close()


def test_all_records_aggregate_independently_of_pages_and_timezone_boundaries(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    try:
        for n in range(61):
            timestamp = datetime(
                2026, 10, 27 if n == 0 else 28, 23 if n == 0 else 0, 59, tzinfo=TZ
            ).timestamp()
            store.usage.begin(
                str(n), created=timestamp, app="notes", model="m", activity_id="activity"
            )
            store.usage.finish(str(n), raw={"prompt_tokens": 10, "completion_tokens": 1})
        report = store.usage.report(period="custom", start="2026-10-28", end="2026-10-28", limit=20)
        assert report["totals"]["calls"] == 60
        assert report["totals"]["input_tokens"] == 600
        assert len(report["items"]) == 20
        second = store.usage.report(
            period="custom",
            start="2026-10-28",
            end="2026-10-28",
            before=report["next_before"],
            limit=20,
        )
        assert second["totals"] == report["totals"]
        assert {r["id"] for r in second["items"]}.isdisjoint(r["id"] for r in report["items"])
        assert report["series"][0]["calls"] == 60
        assert report["groups"]["activity_id"][0]["name"] == "activity"
        assert (
            store.usage.report(
                period="custom", start="2026-10-28", end="2026-10-28", model="other"
            )["totals"]["calls"]
            == 0
        )
        with pytest.raises(ValueError):
            store.usage.report(period="custom", start="2025-01-01", end="2026-10-28")
    finally:
        store.close()


def test_durable_event_replay_no_duplicates_and_correlated_context(tmp_path):
    path = tmp_path / "world.sqlite3"
    store = Store(path)
    store.activity.set("fixture-activity")
    store.event(
        "debug.model_request",
        "request",
        data={
            "request_id": "req",
            "runner": "dsh",
            "model": "m",
            "provider": "p",
            "purpose": "compaction",
            "messages": [],
            "tools": [],
        },
    )
    store.event(
        "debug.model_usage",
        "usage",
        data={
            "request_id": "req",
            "usage": {"inputTokens": 20, "cacheReadTokens": 80, "outputTokens": 10},
        },
    )
    store.event(
        "debug.model_end", "end", data={"request_id": "req", "status": "failed", "duration_ms": 42}
    )
    store.db.execute("DELETE FROM usage_imports WHERE source='events'")
    store.db.commit()
    store.close()
    store = Store(path)
    try:
        items = store.usage.report()["items"]
        assert len(items) == 1
        assert items[0]["input_tokens"] == 100
        assert items[0]["purpose"] == "compaction"
        assert items[0]["status"] == "failed"
        assert items[0]["activity_id"] == "fixture-activity"
        assert items[0]["context_seq"] is not None
    finally:
        store.close()


def test_passive_client_capture_preserves_request_response_and_exception(tmp_path):
    seen = []
    response = SimpleNamespace(
        usage=SimpleNamespace(
            model_dump=lambda: {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": 80,
            }
        )
    )
    sentinel = RuntimeError("synthetic provider failure")

    async def create(**kwargs):
        seen.append(kwargs)
        if len(seen) == 2:
            raise sentinel
        return response

    client = SimpleNamespace(
        base_url="https://user:secret@example/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    reporter = UsageReporter(tmp_path)
    reporter.instrument(client, purpose="chat_planning")
    reporter.instrument(client, purpose="chat_planning")
    request = {
        "model": "protected-model",
        "messages": [{"role": "system", "content": "原字保留。"}],
        "temperature": 0.1,
        "max_tokens": 800,
        "extra_body": {"thinking": {"type": "disabled"}},
    }

    async def run():
        assert await client.chat.completions.create(**request) is response
        with pytest.raises(RuntimeError) as exc:
            await client.chat.completions.create(**request)
        assert exc.value is sentinel

    asyncio.run(run())
    assert seen == [request, request]
    with sqlite3.connect(reporter.path) as db:
        records = [json.loads(r[0]) for r in db.execute("SELECT data FROM reports")]
    assert len(records) == 4
    assert records[0]["provider"] == "example/v1"
    assert records[1]["usage"]["prompt_cache_hit_tokens"] == 80
    assert records[3]["status"] == "failed"
    assert "messages" not in json.dumps(records)


def test_collector_imports_old_counters_once_without_double_counting_new_spool(tmp_path):
    async def run():
        kernel = Kernel(tmp_path)
        kernel.register(NOTES)
        path = kernel.apps["notes"].data_dir
        (path / "cognition").mkdir(parents=True)
        legacy = path / "cognition/astcho.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.execute(
                "CREATE TABLE llm_usage(id INTEGER PRIMARY KEY,model TEXT,prompt_tokens INTEGER,"
                "completion_tokens INTEGER,estimated_cost REAL,created_at REAL)"
            )
            db.execute(
                "INSERT INTO llm_usage VALUES(1,?,?,?,?,?)", ("m", 7, 3, 0, time.time() - 10)
            )
        reporter = UsageReporter(path)
        reporter.emit(
            {
                "event": "start",
                "request_id": "new",
                "created": time.time(),
                "model": "m",
                "provider": "p",
            }
        )
        reporter.emit(
            {
                "event": "end",
                "request_id": "new",
                "status": "succeeded",
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            }
        )
        with sqlite3.connect(legacy) as db:
            db.execute("INSERT INTO llm_usage VALUES(2,?,?,?,?,?)", ("m", 20, 5, 0, time.time()))
        try:
            collector = UsageCollector(kernel)
            collector.sync()
            collector.sync()
            totals = kernel.store.usage.report()["totals"]
            assert totals["calls"] == 2
            assert totals["input_tokens"] == 27
        finally:
            await kernel.close()

    asyncio.run(run())


def test_usage_api_auth_csrf_validation_and_reads_do_not_mutate_knowledge(tmp_path):
    app = create_dashboard_app(config_path=EXAMPLES, data_dir=tmp_path, token=TOKEN)
    with TestClient(app) as client:
        assert client.get("/api/usage").status_code == 401
        assert client.post("/api/usage/rates", json={}).status_code == 401
        client.post("/api/session", json={"token": TOKEN})
        assert client.get("/api/usage?period=bad").status_code == 422
        assert (
            client.post(
                "/api/usage/rates", json={}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/usage/rates",
                json={
                    "provider": "p",
                    "model": "m",
                    "currency": "USD",
                    "input_price": "NaN",
                    "output_price": "2",
                },
            ).status_code
            == 422
        )
        with sqlite3.connect(tmp_path / "world.sqlite3") as db:
            before = list(db.iterdump())
        assert client.get("/api/usage").status_code == 200
        with sqlite3.connect(tmp_path / "world.sqlite3") as db:
            assert list(db.iterdump()) == before
        assert (
            client.post(
                "/api/usage/rates",
                json={
                    "provider": "p",
                    "model": "m",
                    "currency": "USD",
                    "input_price": "1",
                    "output_price": "2",
                },
            ).status_code
            == 200
        )
        with sqlite3.connect(tmp_path / "world.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM usage_rates").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
