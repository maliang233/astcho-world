"""Preview/execute invariants for explicit historical estimates, not actual invoices."""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astcho_world.dashboard import create_dashboard_app
from astcho_world.pricing import presets
from astcho_world.store import Store
from astcho_world.usage_backfill import StalePreview, UsageBackfill, history

SECRET = "synthetic-backfill-secret"


def stamp(text):
    return datetime.fromisoformat(text).timestamp()


def record(
    store, name, at, *, cached=800, raw=True, provider="api.deepseek.com", status="succeeded"
):
    store.usage.begin(name, created=stamp(at), provider=provider, model="deepseek-flash")
    if raw:
        usage = {"prompt_tokens": 1000, "completion_tokens": 100}
        if cached is not None:
            usage["prompt_cache_hit_tokens"] = cached
        store.usage.finish(name, raw=usage, status=status)
    elif status != "running":
        store.usage.finish(name, status=status)
    store.db.commit()


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    record(store, "peak", "2026-10-26T10:00:00+08:00")
    record(store, "off", "2026-10-26T13:00:00+08:00")
    record(store, "missing-cache", "2026-10-26T10:00:00+08:00", cached=None)
    record(store, "missing-usage", "2026-10-26T10:00:00+08:00", raw=False, status="failed")
    record(store, "running", "2026-10-26T10:00:00+08:00", raw=False, status="running")
    record(store, "alias", "2026-10-26T10:00:00+08:00", provider="old-adapter")
    record(store, "other-year", "2025-10-27T10:00:00+08:00")
    rule = store.usage.add_rate(presets()[0])
    # A priced zero must be protected just as any nonzero existing amount.
    store.usage.begin("already-priced", provider="api.deepseek.com", model="deepseek-flash")
    store.usage.finish(
        "already-priced",
        raw={"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0},
    )
    store.db.commit()
    yield store, rule, UsageBackfill(store.db, SECRET)
    store.close()


def test_preview_is_read_only_and_handles_missing_metadata_and_existing_prices(ledger):
    store, rule, service = ledger
    before = list(store.db.iterdump())
    p = service.preview({"rate_id": rule})
    assert list(store.db.iterdump()) == before
    assert p["ready"] == 2
    assert p["skipped"] == {
        "unknown_cache": 1,
        "unknown_usage": 1,
        "running": 1,
        "unknown_schedule": 1,
        "already_priced": 1,
    }
    assert Decimal(p["costs"]["CNY"]) == Decimal("0.001848")
    assert {r["tariff"] for r in p["samples"] if r["state"] == "estimated"} == {"peak", "off_peak"}


def test_apply_preserves_known_costs_records_provenance_and_retries_are_idempotent(ledger):
    store, rule, service = ledger
    before = {r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")}
    p = service.preview({"rate_id": rule})
    result = service.execute(p["preview_token"])
    assert result["updated"] == 2
    rows = {r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")}
    assert Decimal(rows["peak"]["cost"]) == Decimal("0.001232")
    assert Decimal(rows["off"]["cost"]) == Decimal("0.000616")
    for name in [
        "already-priced",
        "running",
        "alias",
        "other-year",
        "missing-cache",
        "missing-usage",
    ]:
        assert rows[name] == before[name]
    selection = json.loads(rows["peak"]["pricing_snapshot"])
    assert selection["backfill"]["label"] == "按补录规则估算"
    assert selection["backfill"]["batch_id"] == result["id"]
    assert selection["rate_id"] == rule
    assert selection["local_time"].startswith("2026-10-26T10:00")
    audit = store.db.execute(
        "SELECT before_json,after_json FROM usage_backfill_items WHERE call_id=?",
        (rows["peak"]["id"],),
    ).fetchone()
    assert json.loads(audit[0])["rate_id"] is None
    assert json.loads(audit[1])["cost"] == rows["peak"]["cost"]
    dumped = list(store.db.iterdump())
    again = service.execute(p["preview_token"])
    assert again["already_applied"] is True
    assert list(store.db.iterdump()) == dumped
    assert len(history(store.db)) == 1
    assert service.preview({"rate_id": rule})["ready"] == 0


def test_preview_excludes_new_calls_and_requires_repreview_if_candidates_change(ledger):
    store, rule, service = ledger
    p = service.preview({"rate_id": rule})
    record(store, "late-import", "2026-10-26T10:00:00+08:00", provider="api.deepseek.com")
    # New rows are beyond the captured high-water mark and don't broaden an approved batch.
    result = service.execute(p["preview_token"])
    assert result["updated"] == 2
    assert (
        store.db.execute("SELECT cost FROM usage_calls WHERE request_id='late-import'").fetchone()[
            0
        ]
        is None
    )
    p = service.preview({"rate_id": rule})
    # Previously missing usage arrives after preview: require a new preview.
    store.usage.finish(
        "missing-usage",
        raw={"prompt_tokens": 2000, "completion_tokens": 10, "prompt_cache_hit_tokens": 0},
    )
    store.db.commit()
    with pytest.raises(StalePreview, match="变化"):
        service.execute(p["preview_token"])


def test_scope_alias_mapping_and_date_filters_are_explicit(ledger):
    store, rule, service = ledger
    assert service.preview({"rate_id": rule, "provider": "not-found"})["matched"] == 0
    p = service.preview(
        {"rate_id": rule, "provider": "old-adapter", "start": "2026-10-26", "end": "2026-10-26"}
    )
    assert p["ready"] == 1
    result = service.execute(p["preview_token"])
    row = store.db.execute("SELECT * FROM usage_calls WHERE request_id='alias'").fetchone()
    assert row["provider"] == "old-adapter"
    mapping = json.loads(row["pricing_snapshot"])["backfill"]
    assert mapping["source_provider"] == "old-adapter"
    assert mapping["rate_provider"] == "api.deepseek.com"
    assert result["scope"]["provider"] == "old-adapter"
    assert service.preview({"rate_id": rule, "start": "2026-10-27"})["ready"] == 0
    with pytest.raises(ValueError, match="日期"):
        service.preview({"rate_id": rule, "start": "2026-10-28", "end": "2026-10-26"})


def test_tamper_expiry_and_rule_change_rejected(ledger, monkeypatch):
    store, rule, service = ledger
    p = service.preview({"rate_id": rule})
    with pytest.raises(ValueError, match="凭证"):
        service.execute(p["preview_token"][:-1] + "x")
    clock = __import__("time").time()
    monkeypatch.setattr("astcho_world.usage_backfill.time.time", lambda: clock + 901)
    with pytest.raises(StalePreview, match="过期"):
        service.execute(p["preview_token"])
    monkeypatch.undo()
    p = service.preview({"rate_id": rule})
    # Rules are immutable in the public API; even an out-of-band edit invalidates a preview.
    store.db.execute("UPDATE usage_rates SET input_price='99' WHERE id=?", (rule,))
    store.db.commit()
    with pytest.raises(StalePreview, match="变化"):
        service.execute(p["preview_token"])


def test_batch_rolls_back_if_any_audit_write_fails(ledger):
    store, rule, service = ledger
    p = service.preview({"rate_id": rule})
    store.db.execute(
        "CREATE TRIGGER reject_audit BEFORE INSERT ON usage_backfill_items "
        "BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    store.db.commit()
    before = list(store.db.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        service.execute(p["preview_token"])
    assert list(store.db.iterdump()) == before


def test_backfill_api_auth_csrf_preview_required_and_restart_audit(tmp_path):
    path = tmp_path / "world.sqlite3"
    store = Store(path)
    record(store, "historic", "2026-10-26T10:00:00+08:00")
    rule = store.usage.add_rate(presets()[0])
    store.close()
    app = create_dashboard_app(
        config_path=Path(__file__).resolve().parents[1] / "examples/apps.json",
        data_dir=tmp_path,
        token=SECRET,
    )
    prefix = "/api/settings/usage-backfill/"
    with TestClient(app) as client:
        assert client.post(prefix + "preview", json={"rate_id": rule}).status_code == 401
        client.post("/api/session", json={"token": SECRET})
        assert client.post(prefix + "execute", json={}).status_code == 422
        assert (
            client.post(
                prefix + "preview",
                json={"rate_id": rule},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        p = client.post(prefix + "preview", json={"rate_id": rule}).json()
        assert p["ready"] == 1
        assert (
            client.post(
                prefix + "execute",
                json={"preview_token": p["preview_token"]},
                headers={"sec-fetch-site": "cross-site"},
            ).status_code
            == 403
        )
        assert (
            client.post(prefix + "execute", json={"preview_token": p["preview_token"]}).json()[
                "updated"
            ]
            == 1
        )
        assert client.get("/api/settings").json()["billing"]["backfills"][0]["updated"] == 1
    app = create_dashboard_app(
        config_path=Path(__file__).resolve().parents[1] / "examples/apps.json",
        data_dir=tmp_path,
        token=SECRET,
    )
    with TestClient(app) as client:
        client.post("/api/session", json={"token": SECRET})
        assert (
            client.post(prefix + "execute", json={"preview_token": p["preview_token"]}).json()[
                "already_applied"
            ]
            is True
        )
        item = client.get("/api/usage?period=custom&start=2026-10-26&end=2026-10-26").json()[
            "items"
        ][0]
        assert item["pricing"]["backfill"]["label"] == "按补录规则估算"
