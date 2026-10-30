"""Proven zero cache counts, incomplete reports and audited historical correction."""

import json
from decimal import Decimal

import pytest

from astcho_world.store import Store
from astcho_world.usage import normalize_usage


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"totalTokens": 1100}, 0),
        ({"totalTokens": 1300, "cacheWriteTokens": 200}, 0),
        ({}, None),
        ({"totalTokens": 1300}, None),  # Read/write split is unknown.
        ({"totalTokens": 1099}, None),
        ({"totalTokens": None}, None),
        ({"totalTokens": True}, None),
        ({"totalTokens": "1100"}, None),
        ({"totalTokens": float("inf")}, None),
        ({"totalTokens": 1100, "cacheReadTokens": None}, None),
        ({"totalTokens": 1100, "cacheReadTokens": -1}, None),
        ({"totalTokens": 1100, "cacheReadTokens": False}, None),
        ({"totalTokens": 1100, "cacheWriteTokens": None}, None),
        ({"totalTokens": 1100, "cacheWriteTokens": -1}, None),
    ],
)
def test_dsh_omitted_read_bucket_requires_exact_zero_remainder(extra, expected):
    usage = normalize_usage({"inputTokens": 1000, "outputTokens": 100, **extra}, dsh=True)
    assert usage["cached"] == expected
    if expected == 0:
        assert usage["input"] == 1000 + extra.get("cacheWriteTokens", 0)


def test_total_does_not_make_missing_input_output_or_openai_cache_known():
    for raw in (
        {"outputTokens": 100, "totalTokens": 100},
        {"inputTokens": 1000, "totalTokens": 1000},
        {"inputTokens": True, "outputTokens": 100, "totalTokens": 101},
    ):
        assert normalize_usage(raw, dsh=True)["cached"] is None
    assert (
        normalize_usage({"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100})[
            "cached"
        ]
        is None
    )
    assert (
        normalize_usage({"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}, dsh=True)["cached"]
        == 0
    )


def test_existing_zero_cache_correction_keeps_original_prices_raw_and_events(tmp_path):
    path = tmp_path / "world.sqlite3"
    store = Store(path)
    u = store.usage
    rate = {
        "provider": "fixture/v1",
        "model": "fixture",
        "currency": "CNY",
        "input_price": "2",
        "cached_price": "0.2",
        "output_price": "6",
    }
    u.add_rate(rate)
    zero = {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100}
    for name, model, raw in (
        ("priced", "fixture", zero),
        ("settled", "fixture", zero),
        ("unpriced", "no-rate", zero),
        ("missing-total", "fixture", {"inputTokens": 1000, "outputTokens": 100}),
        ("unknown-split", "fixture", {**zero, "totalTokens": 1200}),
        ("incomplete", "fixture", None),
        ("cache-hit", "fixture", {**zero, "cacheReadTokens": 800, "totalTokens": 1900}),
    ):
        u.begin(name, source="dsh", provider="fixture/v1", model=model)
        u.finish(name, raw=raw, dsh=True, status="succeeded")
    # Recreate the old stored projections; original source counters remain untouched.
    store.db.execute(
        "UPDATE usage_calls SET cached_tokens=NULL,cost=NULL,cost_state='unknown_cache' "
        "WHERE request_id='priced'"
    )
    store.db.execute(
        "UPDATE usage_calls SET cached_tokens=NULL WHERE request_id IN ('settled','unpriced')"
    )
    u.add_rate({**rate, "input_price": "200", "output_price": "600"})
    store.event("fixture.audit", "Unchanged source event", data={"fixture": True})
    store.db.commit()
    before = {r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")}
    events = [tuple(r) for r in store.db.execute("SELECT * FROM events")]
    assert u.repair_dsh_zero_cache() == 3
    after = {r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")}
    assert after["priced"]["cached_tokens"] == 0
    assert after["priced"]["cost_state"] == "estimated"
    assert Decimal(after["priced"]["cost"]) == Decimal("0.0026")
    assert after["settled"]["cost"] == before["settled"]["cost"]
    assert after["unpriced"]["cost_state"] == "unpriced"
    for name in ("priced", "settled", "unpriced"):
        assert after[name]["cached_tokens"] == 0
        assert {
            k: v
            for k, v in after[name].items()
            if k not in {"cached_tokens", "cost", "currency", "cost_state"}
        } == {
            k: v
            for k, v in before[name].items()
            if k not in {"cached_tokens", "cost", "currency", "cost_state"}
        }
    for name in ("missing-total", "unknown-split", "incomplete", "cache-hit"):
        assert after[name] == before[name]
    audit = list(store.db.execute("SELECT * FROM usage_normalization_repairs"))
    assert len(audit) == 3
    assert all(json.loads(r["before_json"])["cached_tokens"] is None for r in audit)
    assert all(json.loads(r["after_json"])["cached_tokens"] == 0 for r in audit)
    assert [tuple(r) for r in store.db.execute("SELECT * FROM events")] == events
    assert u.report()["totals"]["unknown_cache"] == 3
    assert u.repair_dsh_zero_cache() == 0
    store.close()
    # Startup retries the idempotent correction without changing a second byte.
    store = Store(path)
    try:
        assert {
            r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")
        } == after
        assert (
            store.db.execute("SELECT count(*) FROM usage_normalization_repairs").fetchone()[0] == 3
        )
    finally:
        store.close()
