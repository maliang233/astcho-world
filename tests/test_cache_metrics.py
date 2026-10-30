import pytest

from astcho_world.context_log import record_input
from astcho_world.store import Store
from astcho_world.usage import cache_statistics


def test_rate_is_token_weighted_and_unknown_calls_are_excluded():
    records = [
        {"input_tokens": 100, "cached_tokens": 100},
        {"input_tokens": 900, "cached_tokens": 0},
        {"input_tokens": 1000, "cached_tokens": None},
        {"input_tokens": None, "cached_tokens": 20},
    ]
    result = cache_statistics(records)
    assert result == {
        "hit_rate": 0.1,
        "known_input_tokens": 1000,
        "known_cached_tokens": 100,
        "known_calls": 2,
        "unknown_calls": 2,
        "complete": False,
    }


@pytest.mark.parametrize(
    "records,rate,known",
    [
        ([], None, 0),
        ([{"input_tokens": 0, "cached_tokens": 0}], None, 1),
        ([{"input_tokens": 100, "cached_tokens": 0}], 0.0, 1),
        ([{"input_tokens": 100, "cached_tokens": 100}], 1.0, 1),
        ([{"input_tokens": 1, "cached_tokens": 2}], None, 0),
        ([{"input_tokens": True, "cached_tokens": 0}], None, 0),
    ],
)
def test_empty_zero_and_invalid_counters_are_distinguished(records, rate, known):
    result = cache_statistics(records)
    assert result["hit_rate"] == rate and result["known_calls"] == known


def test_report_rate_uses_full_filter_independent_of_pages_and_exposes_purposes(tmp_path):
    store = Store(tmp_path / "world.sqlite3")
    try:
        for i in range(25):
            purpose = "activity_summary" if i == 0 else "environment"
            store.usage.begin(str(i), purpose=purpose)
            store.usage.finish(
                str(i),
                raw={
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_cache_hit_tokens": 0 if i == 0 else 80,
                },
            )
        report = store.usage.report(limit=5)
        assert report["totals"]["cache"]["hit_rate"] == pytest.approx(1920 / 2500)
        assert len(report["items"]) == 5
        assert report["items"][0]["cache"]["hit_rate"] == 0.8
        groups = {row["name"]: row for row in report["groups"]["purpose"]}
        assert groups["activity_summary"]["cache"]["hit_rate"] == 0
        assert groups["environment"]["cache"]["hit_rate"] == 0.8
        assert store.usage.report(purpose="activity_summary")["totals"]["calls"] == 1
        second = store.usage.report(before=report["items"][-1]["id"], limit=5)
        assert second["totals"] == report["totals"]
    finally:
        store.close()


def test_summary_classification_repair_changes_only_label_and_keeps_audit(tmp_path):
    path = tmp_path / "world.sqlite3"
    store = Store(path)
    try:
        record_input(
            store,
            {
                "request_id": "summary",
                "runner": "dsh",
                "purpose": "activity_summary",
                "messages": [],
                "tools": [],
            },
        )
        store.usage.finish(
            "summary",
            status="succeeded",
            raw={
                "inputTokens": 100,
                "cacheReadTokens": 50,
                "outputTokens": 10,
            },
            dsh=True,
        )
        row = store.db.execute("SELECT * FROM usage_calls").fetchone()
        assert row["purpose"] == "activity_summary"
        # Reproduce the previous derived label, without altering original request data.
        store.db.execute("UPDATE usage_calls SET purpose='environment'")
        store.db.commit()
        before = dict(store.db.execute("SELECT * FROM usage_calls").fetchone())
        events = [tuple(row) for row in store.db.execute("SELECT * FROM events")]
        assert store.usage.repair_activity_summary_purpose() == 1
        after = dict(store.db.execute("SELECT * FROM usage_calls").fetchone())
        assert {**after, "purpose": before["purpose"]} == before
        assert after["purpose"] == "activity_summary"
        assert [tuple(row) for row in store.db.execute("SELECT * FROM events")] == events
        audit = store.db.execute("SELECT * FROM usage_classification_repairs").fetchone()
        assert (
            audit["before_purpose"] == "environment"
            and audit["after_purpose"] == "activity_summary"
        )
        assert store.usage.repair_activity_summary_purpose() == 0
    finally:
        store.close()
    store = Store(path)
    try:
        assert dict(store.db.execute("SELECT * FROM usage_calls").fetchone()) == after
    finally:
        store.close()
