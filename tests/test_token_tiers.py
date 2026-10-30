"""Whole-request tiers use actual total input, including cached tokens."""

import copy
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astcho_world.dashboard import create_dashboard_app
from astcho_world.pricing import (
    estimate_cost,
    presets,
    qwen_presets,
    resolve_tier,
    select_tariff,
    validate_tiers,
)
from astcho_world.store import Store
from astcho_world.usage_backfill import UsageBackfill


def selection(preset=None, at="2026-10-28T10:00:00+08:00"):
    rate = copy.deepcopy(preset or qwen_presets()[0])
    rate.update(
        id=1,
        scheme=json.dumps(rate["scheme"]) if rate["scheme"] else None,
        tiers=json.dumps(rate["tiers"]),
    )
    return select_tariff(rate, datetime.fromisoformat(at).timestamp())


@pytest.mark.parametrize(
    ("tokens", "upper", "price"),
    [
        (0, 32000, "0.2"),
        (32000, 32000, "0.2"),
        (32001, 256000, "0.6"),
        (256000, 256000, "0.6"),
        (256001, 1000000, "1.2"),
        (1000000, 1000000, "1.2"),
    ],
)
def test_exact_boundaries_whole_request_and_output_use_same_tier(tokens, upper, price):
    picked = resolve_tier(selection(), tokens)
    assert picked["tier"]["up_to"] == upper
    assert picked["input_price"] == price
    cost, currency, state = estimate_cost(picked, tokens, 1000, 0)
    expected = (tokens * Decimal(price) + 1000 * Decimal(price) * 4) / 1000000
    assert Decimal(cost) == expected
    assert currency == "CNY" and state == "estimated"
    # This includes 32,001 tokens at the higher rate, not just the final token.


def test_cache_counts_towards_tier_and_missing_or_outside_usage_not_guessed():
    cost, _, state = estimate_cost(selection(), 32001, 1000, 32000)
    assert state == "estimated"
    assert Decimal(cost) == Decimal("0.0062406")
    assert estimate_cost(selection(), 32001, 1000, None)[2] == "unknown_cache"
    assert estimate_cost(selection(), None, 1000, 0)[2] == "unknown_usage"
    assert estimate_cost(selection(), 1000001, 1000, 0)[2] == "unknown_tier"
    missing = resolve_tier(selection(), 1000001)
    assert "input_price" not in missing
    # An explicit final open-ended tier is supported, without a hidden fallback.
    rule = qwen_presets()[0]
    rule["tiers"]["rows"][-1]["up_to"] = None
    assert estimate_cost(selection(rule), 1000001, 1000, 0)[2] == "estimated"


def test_tiers_compose_with_peak_off_peak_and_calendar():
    rule = qwen_presets()[0]
    rule["scheme"] = presets()[0]["scheme"]
    for row in rule["tiers"]["rows"]:
        row["peak"] = {
            k: str(Decimal(row[k]) * 2) for k in ("input_price", "cached_price", "output_price")
        }
    rule["tiers"] = validate_tiers(rule["tiers"], timed=True)
    peak = resolve_tier(selection(rule), 32001)
    off = resolve_tier(selection(rule, "2026-10-28T13:00:00+08:00"), 32001)
    holiday = resolve_tier(selection(rule, "2026-10-01T10:00:00+08:00"), 32001)
    assert peak["tariff"] == "peak" and peak["input_price"] == "1.2"
    assert off["tariff"] == holiday["tariff"] == "off_peak"
    assert off["input_price"] == holiday["input_price"] == "0.6"
    assert estimate_cost(peak, 32001, 1000, 0)[0] == str(
        Decimal(estimate_cost(off, 32001, 1000, 0)[0]) * 2
    )
    assert (
        estimate_cost(selection(rule, "2027-01-04T10:00:00+08:00"), 32001, 1000, 0)[2]
        == "unknown_schedule"
    )


def test_settlement_frozen_rules_repeated_events_and_restart(tmp_path):
    path = tmp_path / "world.sqlite3"
    store = Store(path)
    rule = qwen_presets()[0]
    version = store.usage.add_rate(rule)
    store.usage.begin("request", provider=rule["provider"], model=rule["model"])
    replacement = copy.deepcopy(rule)
    replacement["tiers"]["rows"][1]["input_price"] = "999"
    store.usage.add_rate(replacement)
    # DSH total input includes the separately reported cache reads.
    store.usage.finish(
        "request", raw={"inputTokens": 1, "cacheReadTokens": 32000, "outputTokens": 1000}, dsh=True
    )
    store.usage.finish("request", status="succeeded", duration_ms=100)
    store.db.commit()
    store.close()
    store = Store(path)
    item = store.usage.report(tariff="tiered")["items"][0]
    assert item["rate_id"] == version
    assert item["input_tokens"] == 32001
    assert Decimal(item["cost"]) == Decimal("0.0062406")
    assert item["pricing"]["tier"]["up_to"] == 256000
    assert item["pricing"]["input_price"] == "0.6"
    store.close()


def test_historical_backfill_selects_each_records_actual_input_tier(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    rule = qwen_presets()[0]
    for i, tokens in enumerate([32000, 32001, 256001, 1000001]):
        store.usage.begin(str(i), provider=rule["provider"], model=rule["model"])
        store.usage.finish(
            str(i),
            raw={
                "prompt_tokens": tokens,
                "completion_tokens": 1000,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        )
    version = store.usage.add_rate(rule)
    service = UsageBackfill(store.db, "synthetic-tier-token")
    before = list(store.db.iterdump())
    preview = service.preview({"rate_id": version})
    assert list(store.db.iterdump()) == before
    assert preview["ready"] == 3 and preview["skipped"] == {"unknown_tier": 1}
    assert [r["tier"]["up_to"] for r in preview["samples"][:3]] == [32000, 256000, 1000000]
    result = service.execute(preview["preview_token"])
    assert result["costs"] == preview["costs"]
    assert result["updated"] == 3
    assert service.preview({"rate_id": version})["ready"] == 0
    rows = list(store.db.execute("SELECT pricing_snapshot FROM usage_calls WHERE cost IS NOT NULL"))
    assert all(json.loads(r[0])["tier"]["state"] == "matched" for r in rows)
    store.close()


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"up_to": True}],
        [{"up_to": 1.5}],
        [{"up_to": 0}],
        [{"up_to": 32000}, {"up_to": 32000}],
        [{"up_to": 32000}, {"up_to": 100}],
        [{"up_to": None}, {"up_to": 100}],
    ],
)
def test_invalid_tiers_rejected(rows):
    amount = {"input_price": "1", "output_price": "2", "cached_price": None}
    with pytest.raises(ValueError):
        validate_tiers({"rows": [{**amount, **r} for r in rows]})


def test_api_preserves_tiers_rejects_bad_mode_and_incomplete_peak_prices(tmp_path):
    app = create_dashboard_app(
        config_path=Path(__file__).resolve().parents[1] / "examples/apps.json",
        data_dir=tmp_path,
        token="synthetic-tier-api",
    )
    with TestClient(app) as client:
        client.post("/api/session", json={"token": "synthetic-tier-api"})
        rule = qwen_presets()[0]
        assert client.post("/api/settings/rates", json=rule).status_code == 200
        stored = client.get("/api/settings").json()["billing"]["rates"][0]
        assert stored["tiers"] == rule["tiers"]
        assert client.get("/api/usage?tariff=tiered").status_code == 200
        rule["tiers"]["method"] = "progressive"
        assert client.post("/api/settings/rates", json=rule).status_code == 422
        rule = qwen_presets()[0]
        rule["scheme"] = presets()[0]["scheme"]
        assert client.post("/api/settings/rates", json=rule).status_code == 422
