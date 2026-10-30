"""Local administrative usage ledger. Never supplies information to the subject/model."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from astcho_world.pricing import (
    estimate_cost,
    prices,
    resolve_tier,
    select_tariff,
    validate_scheme,
    validate_tiers,
)

TZ = ZoneInfo("Asia/Shanghai")
MODEL_EVENTS = {"debug.model_request", "debug.model_usage", "debug.model_end"}


def provider_id(url):
    value = urlsplit(str(url))
    if not value.hostname:
        return "unknown"
    port = f":{value.port}" if value.port else ""
    return value.hostname + port + value.path.rstrip("/")


def normalize_usage(raw, *, dsh=False):
    """DSH inputTokens exclude cache read/write; OpenAI prompt_tokens include cache."""
    if not isinstance(raw, dict):
        return {"input": None, "output": None, "cached": None, "reasoning": None}

    def number(value):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return None
        return int(value) if value == int(value) else None

    if dsh:
        read = number(raw.get("cacheReadTokens"))
        write = number(raw.get("cacheWriteTokens"))
        uncached = number(raw.get("inputTokens"))
        output = number(raw.get("outputTokens"))
        total = number(raw.get("totalTokens"))
        # DSH's pi-ai adapter omits zero cache buckets. Its exact total includes
        # disjoint uncached input, cache reads/writes and output. Only infer a
        # missing read bucket when that total proves no tokens remain for it.
        # Missing totals, invalid explicit fields and unexplained remainders stay unknown.
        if (
            "cacheReadTokens" not in raw
            and ("cacheWriteTokens" not in raw or write is not None)
            and uncached is not None
            and output is not None
            and total is not None
            and total == uncached + output + (write or 0)
        ):
            read = 0
        prompt = uncached + (read or 0) + (write or 0) if uncached is not None else None
        reasoning = number(raw.get("reasoningTokens"))
    else:
        prompt = number(raw.get("prompt_tokens", raw.get("input_tokens")))
        output = number(raw.get("completion_tokens", raw.get("output_tokens")))
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        read = number(raw.get("prompt_cache_hit_tokens", details.get("cached_tokens")))
        details = raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
        reasoning = number(details.get("reasoning_tokens"))
    if prompt is not None and read is not None and read > prompt:
        read = None  # Keep malformed provider counters unknown, never silently clamp.
    return {"input": prompt, "output": output, "cached": read, "reasoning": reasoning}


class UsageLedger:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS usage_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, currency TEXT NOT NULL,
                input_price TEXT NOT NULL, cached_price TEXT, output_price TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, app TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
                activity_id TEXT, operation_id TEXT, context_seq INTEGER,
                status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                cached_tokens INTEGER, reasoning_tokens INTEGER, duration_ms INTEGER,
                rate_id INTEGER, cost TEXT, currency TEXT, cost_state TEXT NOT NULL,
                source TEXT NOT NULL, raw TEXT);
            CREATE INDEX IF NOT EXISTS usage_created ON usage_calls(created,id);
            CREATE INDEX IF NOT EXISTS usage_activity ON usage_calls(activity_id);
            CREATE TABLE IF NOT EXISTS usage_imports (source TEXT PRIMARY KEY, cursor INTEGER);
        """)
        # Additive migration: preserve historical rate IDs, costs and unpriced records.
        for table, columns in {
            "usage_rates": {"scheme": "TEXT", "tiers": "TEXT"},
            "usage_calls": {"pricing_snapshot": "TEXT", "tariff": "TEXT"},
        }.items():
            existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        from astcho_world.usage_backfill import install_schema

        install_schema(db)
        self.replay()
        self.repair_dsh_zero_cache()
        self.repair_activity_summary_purpose()

    def repair_activity_summary_purpose(self):
        """Correct derived labels from exact request telemetry, without repricing."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS usage_classification_repairs (
                usage_id INTEGER PRIMARY KEY, created REAL NOT NULL,
                before_purpose TEXT NOT NULL, after_purpose TEXT NOT NULL,
                source_seq INTEGER NOT NULL);
        """)
        rows = self.db.execute("""
            SELECT u.id,u.purpose,e.seq FROM usage_calls u JOIN events e ON e.seq=u.context_seq
            WHERE u.purpose='environment' AND e.kind='debug.model_request'
            AND json_extract(e.data,'$.purpose')='activity_summary'
            AND json_extract(e.data,'$.request_id')=u.request_id
        """).fetchall()
        with self.db:
            for row in rows:
                self.db.execute(
                    "INSERT OR IGNORE INTO usage_classification_repairs VALUES(?,?,?,?,?)",
                    (row["id"], time.time(), row["purpose"], "activity_summary", row["seq"]),
                )
                self.db.execute(
                    "UPDATE usage_calls SET purpose='activity_summary' WHERE id=?", (row["id"],)
                )
        return len(rows)

    def repair_dsh_zero_cache(self):
        """Correct proven SDK zero buckets without repricing unrelated history."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS usage_normalization_repairs (
                usage_id INTEGER NOT NULL, version TEXT NOT NULL, created REAL NOT NULL,
                reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                PRIMARY KEY (usage_id, version));
        """)
        repaired = 0
        with self.db:
            for row in self.db.execute(
                "SELECT * FROM usage_calls WHERE source='dsh' AND cached_tokens IS NULL "
                "AND raw IS NOT NULL"
            ).fetchall():
                try:
                    raw = json.loads(row["raw"])
                except (ValueError, TypeError):
                    continue
                values = normalize_usage(raw, dsh=True)
                if (
                    values["cached"] != 0
                    or values["input"] != row["input_tokens"]
                    or values["output"] != row["output_tokens"]
                ):
                    continue
                before = {
                    key: row[key] for key in ("cached_tokens", "cost", "currency", "cost_state")
                }
                after = {**before, "cached_tokens": 0}
                # Resolve only the version already attached to this call, including
                # historical backfill snapshots. Never select a newer configured price.
                if row["cost_state"] == "unknown_cache":
                    selection = (
                        json.loads(row["pricing_snapshot"]) if row["pricing_snapshot"] else None
                    )
                    if selection is None and row["rate_id"]:
                        rate = self.db.execute(
                            "SELECT * FROM usage_rates WHERE id=?", (row["rate_id"],)
                        ).fetchone()
                        selection = select_tariff(dict(rate), row["created"]) if rate else None
                    if selection is not None:
                        cost, currency, state = estimate_cost(
                            selection, values["input"], values["output"], 0
                        )
                        after.update(cost=cost, currency=currency, cost_state=state)
                self.db.execute(
                    "INSERT OR IGNORE INTO usage_normalization_repairs VALUES(?,?,?,?,?,?)",
                    (
                        row["id"],
                        "dsh-exact-zero-v1",
                        time.time(),
                        "DSH exact total proves omitted cache-read bucket is zero",
                        json.dumps(before),
                        json.dumps(after),
                    ),
                )
                self.db.execute(
                    "UPDATE usage_calls SET cached_tokens=?,cost=?,currency=?,cost_state=? "
                    "WHERE id=?",
                    (
                        after["cached_tokens"],
                        after["cost"],
                        after["currency"],
                        after["cost_state"],
                        row["id"],
                    ),
                )
                repaired += 1
        return repaired

    def replay(self):
        row = self.db.execute("SELECT cursor FROM usage_imports WHERE source='events'").fetchone()
        cursor = row[0] if row else 0
        for event in self.db.execute(
            "SELECT * FROM events WHERE seq>? AND kind IN "
            "('debug.model_request','debug.model_usage','debug.model_end') ORDER BY seq",
            (cursor,),
        ).fetchall():
            self.event(dict(event))
        self.db.commit()

    def event(self, event):
        data = json.loads(event["data"])
        request_id = data.get("request_id") or f"event:{event['seq']}"
        if event["kind"] == "debug.model_request":
            self.begin(
                request_id,
                created=event["created"],
                app=event["app"],
                provider=data.get("billing_provider", data.get("provider")) or "unknown",
                model=data.get("model") or "unknown",
                purpose=(
                    data["purpose"]
                    if data.get("purpose") in {"compaction", "activity_summary"}
                    else "environment"
                ),
                activity_id=event["activity_id"],
                context_seq=event["seq"],
                source=data.get("runner", "native"),
            )
        elif self.db.execute(
            "SELECT 1 FROM usage_calls WHERE request_id=?", (request_id,)
        ).fetchone():
            if event["kind"] == "debug.model_usage":
                self.finish(
                    request_id, raw=data.get("usage"), dsh=data.get("format", "dsh") == "dsh"
                )
            else:
                self.finish(
                    request_id,
                    status=data.get("status", "succeeded"),
                    duration_ms=data.get("duration_ms"),
                )
        self.db.execute("INSERT OR REPLACE INTO usage_imports VALUES('events',?)", (event["seq"],))

    def begin(
        self,
        request_id,
        *,
        created=None,
        app="world",
        provider="unknown",
        model="unknown",
        purpose="unclassified",
        activity_id=None,
        operation_id=None,
        context_seq=None,
        source="plugin",
    ):
        created = time.time() if created is None else created
        rate = self.db.execute(
            "SELECT * FROM usage_rates WHERE provider=? AND model=? AND created<=? "
            "ORDER BY created DESC,id DESC LIMIT 1",
            (provider, model, created),
        ).fetchone()
        selection = select_tariff(dict(rate), created) if rate else None
        self.db.execute(
            "INSERT OR IGNORE INTO usage_calls(request_id,created,updated,app,provider,"
            "model,purpose,"
            "activity_id,operation_id,context_seq,status,rate_id,cost_state,source,"
            "pricing_snapshot,tariff) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'running',?,'unknown_usage',?,?,?)",
            (
                request_id,
                created,
                created,
                app,
                provider,
                model,
                purpose,
                activity_id,
                operation_id,
                context_seq,
                rate["id"] if rate else None,
                source,
                json.dumps(selection) if selection else None,
                selection["tariff"] if selection else None,
            ),
        )

    def finish(self, request_id, *, raw=None, dsh=False, status=None, duration_ms=None):
        row = self.db.execute(
            "SELECT * FROM usage_calls WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Usage request must be admitted before settlement")
        values = (
            normalize_usage(raw, dsh=dsh)
            if raw is not None
            else {
                "input": row["input_tokens"],
                "output": row["output_tokens"],
                "cached": row["cached_tokens"],
                "reasoning": row["reasoning_tokens"],
            }
        )
        rate = json.loads(row["pricing_snapshot"]) if row["pricing_snapshot"] else None
        if rate is None and row["rate_id"]:
            legacy = self.db.execute(
                "SELECT * FROM usage_rates WHERE id=?", (row["rate_id"],)
            ).fetchone()
            rate = select_tariff(dict(legacy), row["created"]) if legacy else None
        rate = resolve_tier(rate, values["input"])
        cost, currency, state = estimate_cost(
            rate, values["input"], values["output"], values["cached"]
        )
        self.db.execute(
            "UPDATE usage_calls SET updated=?,status=?,input_tokens=?,output_tokens=?,"
            "cached_tokens=?,reasoning_tokens=?,duration_ms=?,cost=?,currency=?,cost_state=?,raw=?,"
            "pricing_snapshot=? "
            "WHERE request_id=?",
            (
                time.time(),
                status or ("succeeded" if row["status"] == "running" else row["status"]),
                values["input"],
                values["output"],
                values["cached"],
                values["reasoning"],
                duration_ms if duration_ms is not None else row["duration_ms"],
                cost,
                currency,
                state,
                json.dumps(raw) if raw is not None else row["raw"],
                json.dumps(rate) if rate else None,
                request_id,
            ),
        )

    def add_rate(self, value):
        provider, model = (
            str(value.get("provider", "")).strip(),
            str(value.get("model", "")).strip(),
        )
        currency = str(value.get("currency", "")).upper()
        if not provider or not model or len(provider) > 200 or len(model) > 200:
            raise ValueError("请填写服务商标识与模型名称（最多 200 字）")
        if currency not in {"CNY", "USD", "EUR", "JPY", "GBP", "HKD"}:
            raise ValueError("不支持的币种")
        scheme = validate_scheme(value.get("scheme"))
        tiers = validate_tiers(value.get("tiers"), timed=scheme is not None)
        amounts = prices(tiers["rows"][0] if tiers else scheme["off_peak"] if scheme else value)
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO usage_rates(created,provider,model,currency,input_price,cached_price,"
                "output_price,scheme,tiers) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    provider,
                    model,
                    currency,
                    amounts["input_price"],
                    amounts["cached_price"],
                    amounts["output_price"],
                    json.dumps(scheme) if scheme else None,
                    json.dumps(tiers) if tiers else None,
                ),
            )
        return cursor.lastrowid

    def rates(self):
        return [
            {
                **dict(r),
                "scheme": json.loads(r["scheme"]) if r["scheme"] else None,
                "tiers": json.loads(r["tiers"]) if r["tiers"] else None,
            }
            for r in self.db.execute("SELECT * FROM usage_rates ORDER BY id DESC")
        ]

    def today_totals(self):
        low = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return aggregate(
            [
                dict(r)
                for r in self.db.execute(
                    "SELECT input_tokens,output_tokens,cached_tokens,cost,currency,cost_state "
                    "FROM usage_calls WHERE created>=? AND created<?",
                    (low, low + 86400),
                )
            ]
        )

    def report(
        self,
        *,
        period="today",
        start="",
        end="",
        provider="",
        model="",
        purpose="",
        app="",
        activity_id="",
        tariff="",
        before=0,
        limit=50,
    ):
        now = datetime.now(TZ)
        if period == "custom":
            try:
                low = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=TZ)
                high = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=TZ) + timedelta(days=1)
            except ValueError:
                raise ValueError("请选择有效的起止日期") from None
        else:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            low = {
                "today": midnight,
                "month": midnight.replace(day=1),
                "7d": midnight - timedelta(days=6),
                "30d": midnight - timedelta(days=29),
            }.get(period)
            if low is None:
                raise ValueError("未知统计时段")
            high = midnight + timedelta(days=1)
        if high <= low or (high - low).days > 366:
            raise ValueError("统计范围必须为 1 至 366 天")
        clauses, args = ["created>=?", "created<?"], [low.timestamp(), high.timestamp()]
        for key, value in (
            ("provider", provider),
            ("model", model),
            ("purpose", purpose),
            ("app", app),
            ("activity_id", activity_id),
        ):
            if key == "activity_id" and value == "__unclassified__":
                clauses.append("activity_id IS NULL")
                continue
            if value:
                clauses.append(f"{key}=?")
                args.append(value)
        if tariff:
            if tariff not in {"fixed", "tiered", "peak", "off_peak", "unknown", "unpriced"}:
                raise ValueError("未知计费时段")
            clauses.append(
                "coalesce(tariff,CASE WHEN rate_id IS NULL THEN 'unpriced' ELSE 'fixed' END)=?"
            )
            args.append(tariff)
        # Aggregate the entire filtered range, independently of detail pagination.
        records = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM usage_calls WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created DESC,id DESC",
                args,
            )
        ]
        totals = aggregate(records)
        groups = {}
        for key in ("purpose", "model", "provider", "app", "activity_id", "tariff"):
            buckets = defaultdict(list)
            for item in records:
                label = item[key] or "unclassified"
                if key == "tariff":
                    label = item[key] or ("fixed" if item["rate_id"] else "unpriced")
                buckets[label].append(item)
            groups[key] = [{"name": k, **aggregate(v)} for k, v in buckets.items()]
        by_day = defaultdict(list)
        for item in records:
            by_day[datetime.fromtimestamp(item["created"], TZ).strftime("%Y-%m-%d")].append(item)
        series, day = [], low
        while day < high:
            label = day.strftime("%Y-%m-%d")
            series.append({"date": label, **aggregate(by_day[label])})
            day += timedelta(days=1)
        pivot = (
            self.db.execute("SELECT created,id FROM usage_calls WHERE id=?", (before,)).fetchone()
            if before
            else None
        )
        items = [
            r for r in records if not before or (pivot and (r["created"], r["id"]) < tuple(pivot))
        ][: min(max(limit, 1), 100)]
        for item in items:
            item["cache"] = cache_statistics([item])
            pricing = self.db.execute(
                "SELECT * FROM usage_rates WHERE id=?", (item["rate_id"],)
            ).fetchone()
            item["pricing"] = (
                json.loads(item["pricing_snapshot"])
                if item["pricing_snapshot"]
                else select_tariff(dict(pricing), item["created"])
                if pricing
                else None
            )
            item["tariff"] = item["tariff"] or ("fixed" if item["rate_id"] else "unpriced")
            item.pop("pricing_snapshot", None)
            item["raw"] = json.loads(item["raw"]) if item["raw"] else None
        options = {
            key: [
                r[0]
                for r in self.db.execute(f"SELECT DISTINCT {key} FROM usage_calls ORDER BY {key}")
            ]
            for key in ("provider", "model", "purpose", "app")
        }
        return {
            "totals": totals,
            "groups": groups,
            "series": series,
            "items": items,
            "next_before": items[-1]["id"] if len(items) == min(max(limit, 1), 100) else None,
            "options": options,
            "timezone": str(TZ),
            "start": low.date().isoformat(),
            "end": (high - timedelta(days=1)).date().isoformat(),
            "rates": self.rates(),
        }


def aggregate(records):
    costs = defaultdict(Decimal)
    for item in records:
        if item["cost"] is not None:
            costs[item["currency"]] += Decimal(item["cost"])
    return {
        "calls": len(records),
        "cache": cache_statistics(records),
        **{
            key: sum(r[key] or 0 for r in records)
            for key in ("input_tokens", "output_tokens", "cached_tokens")
        },
        "unknown_usage": sum(
            r["input_tokens"] is None or r["output_tokens"] is None for r in records
        ),
        "unknown_cache": sum(r["cached_tokens"] is None for r in records),
        "unpriced": sum(r["cost_state"] == "unpriced" for r in records),
        "unsettled": sum(r["cost_state"] != "estimated" for r in records),
        "costs": {k: str(v) for k, v in costs.items()},
    }


def cache_statistics(records):
    """Token-weighted hit rate over calls with both valid input and cache counts."""
    eligible = []
    for record in records:
        total, hit = record.get("input_tokens"), record.get("cached_tokens")
        if (
            type(total) is int and type(hit) is int
            and 0 <= hit <= total
        ):
            eligible.append((total, hit))
    inputs = sum(total for total, _ in eligible)
    cached = sum(hit for _, hit in eligible)
    return {
        "hit_rate": cached / inputs if inputs else None,
        "known_input_tokens": inputs,
        "known_cached_tokens": cached,
        "known_calls": len(eligible),
        "unknown_calls": len(records) - len(eligible),
        "complete": len(eligible) == len(records),
    }


class UsageCollector:
    """Read only app-owned telemetry spools and pre-existing legacy counters."""

    def __init__(self, kernel):
        self.kernel = kernel

    def sync(self):
        import logging

        for app_id, process in self.kernel.apps.items():
            try:
                with self.kernel.store.db:
                    self._sync_app(app_id, process)
            except Exception:
                logging.getLogger(__name__).exception("Usage import failed for app %s", app_id)

    def _sync_app(self, app_id, process):
        ledger, db = self.kernel.store.usage, self.kernel.store.db
        spool = process.data_dir / "usage-spool.sqlite3"
        boundary = None
        if spool.is_file():
            with sqlite3.connect(f"{spool.resolve().as_uri()}?mode=ro", uri=True) as source:
                source.row_factory = sqlite3.Row
                row = source.execute("SELECT value FROM metadata WHERE key='installed'").fetchone()
                boundary = float(row[0]) if row else None
                key = f"spool:{app_id}"
                cursor = db.execute(
                    "SELECT cursor FROM usage_imports WHERE source=?", (key,)
                ).fetchone()
                for row in source.execute(
                    "SELECT * FROM reports WHERE seq>? ORDER BY seq",
                    (cursor[0] if cursor else 0,),
                ):
                    data = json.loads(row["data"])
                    rid = f"plugin:{app_id}:{data['request_id']}"
                    if data["event"] == "start":
                        activity = db.execute(
                            "SELECT activity_id FROM events WHERE operation_id=? "
                            "AND kind='operation.started' ORDER BY seq DESC LIMIT 1",
                            (data.get("operation_id"),),
                        ).fetchone()
                        ledger.begin(
                            rid,
                            app=app_id,
                            created=data["created"],
                            provider=data["provider"],
                            model=data["model"],
                            purpose=data.get("purpose", "unclassified"),
                            operation_id=data.get("operation_id"),
                            activity_id=activity[0] if activity else None,
                            source="plugin",
                        )
                    else:
                        ledger.finish(
                            rid,
                            raw=data.get("usage"),
                            status=data["status"],
                            duration_ms=data.get("duration_ms"),
                        )
                    db.execute(
                        "INSERT OR REPLACE INTO usage_imports VALUES(?,?)", (key, row["seq"])
                    )
        if process.process is None or process.process.returncode is not None:
            db.execute(
                "UPDATE usage_calls SET status='interrupted',updated=? "
                "WHERE app=? AND source='plugin' AND status='running'",
                (time.time(), app_id),
            )
        legacy = process.data_dir / "cognition" / "astcho.sqlite3"
        if legacy.is_file():
            with sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True) as source:
                source.row_factory = sqlite3.Row
                key = f"legacy:{app_id}"
                cursor = db.execute(
                    "SELECT cursor FROM usage_imports WHERE source=?", (key,)
                ).fetchone()
                query = "SELECT * FROM llm_usage WHERE id>?"
                values = [cursor[0] if cursor else 0]
                if boundary is not None:
                    query += " AND created_at<?"
                    values.append(boundary)
                for row in source.execute(query + " ORDER BY id", values):
                    rid = f"legacy:{app_id}:{row['id']}"
                    ledger.begin(
                        rid,
                        created=row["created_at"],
                        app=app_id,
                        model=row["model"],
                        source="legacy_history",
                    )
                    ledger.finish(
                        rid,
                        raw={
                            "prompt_tokens": row["prompt_tokens"],
                            "completion_tokens": row["completion_tokens"],
                        },
                    )
                    db.execute("INSERT OR REPLACE INTO usage_imports VALUES(?,?)", (key, row["id"]))

    async def run(self):
        import asyncio
        import logging

        while True:
            try:
                self.sync()
            except Exception:
                logging.getLogger(__name__).exception("Usage telemetry import failed")
            await asyncio.sleep(2)


def seed_demo_usage(store):
    """Synthetic rates and counters, only used by the explicitly disposable --demo server."""
    if store.db.execute("SELECT 1 FROM usage_calls WHERE source='demo'").fetchone():
        return
    ledger = store.usage
    now = time.time()
    for provider, model, currency, plain, cached, output in (
        ("demo.provider/v1", "demo-chat", "CNY", "2", "0.2", "6"),
        ("demo.provider/v1", "demo-vision", "USD", "1", None, "4"),
    ):
        store.db.execute(
            "INSERT INTO usage_rates(created,provider,model,currency,input_price,cached_price,"
            "output_price) VALUES(?,?,?,?,?,?,?)",
            (now - 30 * 86400, provider, model, currency, plain, cached, output),
        )
    for index in range(14):
        rid = f"demo-usage:{index}"
        ledger.begin(
            rid,
            created=now - (index // 2) * 86400,
            app="qq" if index % 3 == 0 else "world",
            provider="demo.provider/v1",
            model="demo-vision" if index % 3 == 0 else "demo-chat",
            purpose=["vision", "environment", "compaction"][index % 3],
            source="demo",
            activity_id=f"demo-activity:{index // 2}",
        )
        ledger.finish(
            rid,
            raw={
                "prompt_tokens": 1000 + index * 345,
                "completion_tokens": 100 + index * 14,
                "prompt_cache_hit_tokens": 800 + index * 200,
            },
            status="failed" if index == 5 else "succeeded",
            duration_ms=450 + index * 170,
        )
    from astcho_world.pricing import presets

    timed = presets()[0]
    timed.update(provider="demo.provider/v1", model="demo-timed")
    timed["scheme"]["source"] = "合成演示，非正式计价配置"
    rule_id = ledger.add_rate(timed)
    store.db.execute("UPDATE usage_rates SET created=? WHERE id=?", (now - 30 * 86400, rule_id))
    sample = datetime.now(TZ) - timedelta(days=2)
    while sample.weekday() > 4:
        sample -= timedelta(days=1)
    for hour in (10, 13):
        rid = f"demo-timed:{hour}"
        ledger.begin(
            rid,
            created=sample.replace(hour=hour, minute=0, second=0).timestamp(),
            provider="demo.provider/v1",
            model="demo-timed",
            purpose="environment",
            source="demo",
        )
        ledger.finish(
            rid,
            raw={
                "prompt_tokens": 10000,
                "prompt_cache_hit_tokens": 8000,
                "completion_tokens": 1000,
            },
            duration_ms=1200,
        )
    # Unpriced history precedes a newly configured rule, for preview/execute QA only.
    for hour in (10, 13):
        rid = f"demo-backfill:{hour}"
        ledger.begin(
            rid,
            created=sample.replace(hour=hour, minute=0, second=0).timestamp(),
            provider="demo.provider/v1",
            model="demo-history",
            purpose="environment",
            source="demo",
        )
        ledger.finish(
            rid,
            raw={"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "completion_tokens": 100},
            duration_ms=600,
        )
    history_rule = presets()[0]
    history_rule.update(provider="demo.provider/v1", model="demo-history")
    history_rule["scheme"]["source"] = "合成演示，非正式计价配置"
    ledger.add_rate(history_rule)
    ledger.begin("demo-missing-usage", app="world", model="demo-chat", source="demo")
    ledger.finish("demo-missing-usage", status="failed")
    store.db.commit()
