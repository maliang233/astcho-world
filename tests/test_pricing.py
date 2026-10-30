"""Time-of-use accounting: boundaries, calendars, frozen prices and additive migration."""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astcho_world.dashboard import create_dashboard_app
from astcho_world.pricing import presets, select_tariff, validate_scheme
from astcho_world.store import Store
from astcho_world.usage import UsageLedger


def stamp(text):
    return datetime.fromisoformat(text).timestamp()


def selected(text, scheme=None):
    config = presets()[0]["scheme"] if scheme is None else validate_scheme(scheme)
    return select_tariff({"id": 1, "currency": "CNY", "scheme": json.dumps(config)}, stamp(text))


@pytest.mark.parametrize(
    ("time", "expected"),
    [
        ("2026-10-28T08:59:59+08:00", "off_peak"),
        ("2026-10-28T09:00:00+08:00", "peak"),
        ("2026-10-28T11:59:59+08:00", "peak"),
        ("2026-10-28T12:00:00+08:00", "off_peak"),
        ("2026-10-28T13:59:59+08:00", "off_peak"),
        ("2026-10-28T14:00:00+08:00", "peak"),
        ("2026-10-28T18:00:00+08:00", "off_peak"),
        ("2026-10-24T10:00:00+08:00", "off_peak"),
        ("2026-10-01T10:00:00+08:00", "off_peak"),
        ("2026-10-10T10:00:00+08:00", "off_peak"),  # Adjusted work Saturday stays off-peak.
        ("2026-10-28T01:00:00+00:00", "peak"),
        ("2027-01-04T10:00:00+08:00", "unknown"),
    ],
)
def test_deepseek_calendar_and_exact_boundaries(time, expected):
    assert selected(time)["tariff"] == expected


def test_cross_midnight_cross_week_and_timezone():
    scheme = presets()[0]["scheme"]
    scheme.update(
        timezone="UTC", calendar=None, windows=[{"days": [6], "start": "22:00", "end": "02:00"}]
    )
    assert selected("2026-10-25T21:59:59Z", scheme)["tariff"] == "off_peak"
    assert selected("2026-10-25T22:00:00Z", scheme)["tariff"] == "peak"
    assert selected("2026-10-26T01:59:59Z", scheme)["tariff"] == "peak"
    assert selected("2026-10-26T02:00:00Z", scheme)["tariff"] == "off_peak"
    scheme["windows"] = [{"days": [0], "start": "00:00", "end": "24:00"}]
    assert selected("2026-10-26T23:59:59Z", scheme)["tariff"] == "peak"


def test_scheme_validation_overlap_and_calendar_coverage():
    scheme = presets()[0]["scheme"]
    scheme["windows"] = [
        {"days": [6], "start": "22:00", "end": "02:00"},
        {"days": [0], "start": "01:00", "end": "03:00"},
    ]
    with pytest.raises(ValueError, match="重叠"):
        validate_scheme(scheme)
    scheme = presets()[0]["scheme"]
    scheme["timezone"] = "not/a/timezone"
    with pytest.raises(ValueError, match="时区"):
        validate_scheme(scheme)
    scheme = presets()[0]["scheme"]
    scheme["peak"]["input_price"] = "-1"
    with pytest.raises(ValueError, match="非负"):
        validate_scheme(scheme)
    scheme = presets()[0]["scheme"]
    scheme["calendar"]["off_peak_dates"].append("2027-01-01")
    with pytest.raises(ValueError, match="已核对年份"):
        validate_scheme(scheme)


def test_request_price_frozen_before_boundary_and_unknown_year_never_guessed(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    try:
        preset = presets()[0]
        version = store.usage.add_rate(preset)
        # Fixture version was active before the synthetic request dates.
        store.db.execute(
            "UPDATE usage_rates SET created=? WHERE id=?", (stamp("2026-01-01T00:00:00Z"), version)
        )
        store.usage.begin(
            "before",
            created=stamp("2026-10-28T11:59:59+08:00"),
            provider=preset["provider"],
            model=preset["model"],
        )
        updated = presets()[0]
        updated["scheme"]["peak"]["input_price"] = "200"
        store.usage.add_rate(updated)
        store.usage.finish(
            "before",
            raw={"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "completion_tokens": 100},
        )
        store.usage.begin(
            "holiday",
            created=stamp("2026-10-01T10:00:00+08:00"),
            provider=preset["provider"],
            model=preset["model"],
        )
        store.usage.finish(
            "holiday",
            raw={"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "completion_tokens": 100},
        )
        store.usage.begin(
            "missing",
            created=stamp("2027-01-04T10:00:00+08:00"),
            provider=preset["provider"],
            model=preset["model"],
        )
        store.usage.finish("missing", raw={"prompt_tokens": 1000, "completion_tokens": 100})
        rows = {r["request_id"]: dict(r) for r in store.db.execute("SELECT * FROM usage_calls")}
        assert rows["before"]["tariff"] == "peak"
        assert rows["before"]["rate_id"] == version
        assert Decimal(rows["before"]["cost"]) == Decimal("0.001232")
        assert Decimal(rows["holiday"]["cost"]) == Decimal("0.000616")
        assert rows["missing"]["cost_state"] == "unknown_schedule"
        assert rows["missing"]["cost"] is None
        report = store.usage.report(
            period="custom", start="2026-10-28", end="2026-10-28", tariff="peak"
        )
        assert report["totals"]["calls"] == 1
        assert report["items"][0]["pricing"]["input_price"] == "2"
        assert report["groups"]["tariff"][0]["name"] == "peak"
    finally:
        store.close()


def test_additive_upgrade_preserves_historical_cost_and_unpriced_rows(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    store.usage.add_rate(
        {"provider": "p", "model": "m", "currency": "USD", "input_price": "1", "output_price": "2"}
    )
    store.usage.begin("fixed", provider="p", model="m")
    store.usage.finish("fixed", raw={"prompt_tokens": 1000, "completion_tokens": 100})
    store.usage.begin("unpriced", provider="other", model="m")
    store.usage.finish("unpriced", raw={"prompt_tokens": 1000, "completion_tokens": 100})
    store.db.commit()
    # Simulate the preceding schema without the new columns.
    store.db.execute("ALTER TABLE usage_rates DROP COLUMN scheme")
    store.db.execute("ALTER TABLE usage_rates DROP COLUMN tiers")
    store.db.execute("ALTER TABLE usage_calls DROP COLUMN pricing_snapshot")
    store.db.execute("ALTER TABLE usage_calls DROP COLUMN tariff")
    store.db.commit()
    before = list(store.db.execute("SELECT request_id,cost,rate_id,cost_state FROM usage_calls"))
    ledger = UsageLedger(store.db)
    after = list(store.db.execute("SELECT request_id,cost,rate_id,cost_state FROM usage_calls"))
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    report = ledger.report()
    assert {r["tariff"] for r in report["items"]} == {"fixed", "unpriced"}
    store.close()


def test_categorized_settings_auth_secrets_and_version_write(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTCHO_WORLD_API_KEY", "never-show-this-secret")
    monkeypatch.setenv(
        "ASTCHO_WORLD_BASE_URL", "https://user:private-pass@api.example/v1?token=hidden"
    )
    token = "synthetic-settings-token"
    app = create_dashboard_app(
        config_path=Path(__file__).resolve().parents[1] / "examples/apps.json",
        data_dir=tmp_path,
        token=token,
    )
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401
        client.post("/api/session", json={"token": token})
        with sqlite3.connect(tmp_path / "world.sqlite3") as db:
            before = list(db.iterdump())
        reply = client.get("/api/settings")
        assert reply.status_code == 200
        data = reply.json()
        assert [r["id"] for r in data["categories"]] == [
            "billing",
            "models",
            "apps",
            "interface",
            "presentation",
        ]
        assert "never-show-this-secret" not in reply.text
        assert "private-pass" not in reply.text
        assert "token=hidden" not in reply.text
        assert data["models"][0]["provider"] == "api.example/v1"
        assert data["billing"]["presets"][0]["scheme"]["calendar"]["years"] == [2026]
        with sqlite3.connect(tmp_path / "world.sqlite3") as db:
            assert list(db.iterdump()) == before
        assert (
            client.post(
                "/api/settings/rates", json=presets()[0], headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.post("/api/settings/rates", json=presets()[0]).status_code == 200
        assert (
            client.get("/api/settings").json()["billing"]["rates"][0]["scheme"]["kind"]
            == "time_of_use"
        )
        assert client.get("/api/usage?tariff=invalid").status_code == 422
