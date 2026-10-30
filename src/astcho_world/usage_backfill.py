"""Explicit, previewed historical estimates; existing monetary amounts are immutable."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from astcho_world.pricing import estimate_cost, resolve_tier, select_tariff

TZ = ZoneInfo("Asia/Shanghai")
BILLING_FIELDS = (
    "rate_id",
    "pricing_snapshot",
    "tariff",
    "cost",
    "currency",
    "cost_state",
    "updated",
)


class StalePreview(ValueError):
    pass


def install_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS usage_backfill_batches (
            id TEXT PRIMARY KEY, created REAL NOT NULL, executed REAL NOT NULL,
            scope TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage_backfill_items (
            batch_id TEXT NOT NULL, call_id INTEGER NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL,
            PRIMARY KEY(batch_id,call_id));
    """)


def history(db):
    return [
        json.loads(row[0])
        for row in db.execute(
            "SELECT result FROM usage_backfill_batches ORDER BY executed DESC,id DESC LIMIT 10"
        )
    ]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class UsageBackfill:
    def __init__(self, db, secret):
        self.db = db
        self.secret = secret.encode()

    def _scope(self, value):
        if not isinstance(value, dict) or type(value.get("rate_id")) is not int:
            raise ValueError("请选择已保存的计价规则版本")
        rate = self.db.execute(
            "SELECT * FROM usage_rates WHERE id=?", (value["rate_id"],)
        ).fetchone()
        if rate is None:
            raise ValueError("计价规则版本不存在")
        provider = value.get("provider", rate["provider"])
        if not isinstance(provider, str) or not provider.strip() or len(provider) > 200:
            raise ValueError("请选择历史记录中的服务商标识")
        scope = {
            "rate_id": rate["id"],
            "provider": provider.strip(),
            "start": "",
            "end": "",
            "cutoff_id": self.db.execute("SELECT coalesce(max(id),0) FROM usage_calls").fetchone()[
                0
            ],
            "cutoff_time": time.time(),
        }
        for key in ("start", "end"):
            text = value.get(key, "")
            if text:
                try:
                    parsed = datetime.strptime(text, "%Y-%m-%d")
                    if parsed.strftime("%Y-%m-%d") != text:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ValueError("日期格式应为 YYYY-MM-DD") from None
                scope[key] = text
        if scope["start"] and scope["end"] and scope["start"] > scope["end"]:
            raise ValueError("开始日期不能晚于结束日期")
        return scope

    def _plan(self, scope):
        rate = self.db.execute(
            "SELECT * FROM usage_rates WHERE id=?", (scope["rate_id"],)
        ).fetchone()
        if rate is None:
            raise ValueError("计价规则版本已不存在")
        rule = dict(rate)
        clauses = ["provider=?", "model=?", "id<=?", "created<=?"]
        params = [scope["provider"], rate["model"], scope["cutoff_id"], scope["cutoff_time"]]
        for key in ("start", "end"):
            if scope[key]:
                date = datetime.strptime(scope[key], "%Y-%m-%d").replace(tzinfo=TZ)
                if key == "end":
                    date += timedelta(days=1)
                clauses.append("created>=?" if key == "start" else "created<?")
                params.append(date.timestamp())
        rows = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM usage_calls WHERE " + " AND ".join(clauses) + " ORDER BY created,id",
                params,
            )
        ]
        fingerprint = hashlib.sha256(
            encode({"rule": rule, "rows": rows, "scope": scope}).encode()
        ).hexdigest()
        ready, samples, skipped = [], [], Counter()
        total = Decimal(0)
        for row in rows:
            if row["cost"] is not None or row["cost_state"] == "estimated":
                skipped["already_priced"] += 1
                continue
            if row["status"] == "running":
                skipped["running"] += 1
                continue
            selection = resolve_tier(select_tariff(rule, row["created"]), row["input_tokens"])
            cost, currency, state = estimate_cost(
                selection, row["input_tokens"], row["output_tokens"], row["cached_tokens"]
            )
            if state != "estimated":
                skipped[state] += 1
            else:
                total += Decimal(cost)
                ready.append(
                    {"row": row, "selection": selection, "cost": cost, "currency": currency}
                )
            if len(samples) < 50:
                samples.append(
                    {
                        "id": row["id"],
                        "created": row["created"],
                        "model": row["model"],
                        "provider": row["provider"],
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                        "cached_tokens": row["cached_tokens"],
                        "previous_state": row["cost_state"],
                        "state": state,
                        "tariff": selection["tariff"],
                        "tier": selection.get("tier"),
                        "reason": selection["reason"],
                        "cost": cost,
                        "currency": currency,
                    }
                )
        result = {
            "matched": len(rows),
            "ready": len(ready),
            "skipped": dict(skipped),
            "costs": {rate["currency"]: str(total)} if ready else {},
            "samples": samples,
            "sample_limit": 50,
            "rate": {
                "id": rate["id"],
                "created": rate["created"],
                "provider": rate["provider"],
                "model": rate["model"],
                "currency": rate["currency"],
            },
            "scope": scope,
        }
        return fingerprint, ready, result

    def preview(self, value):
        scope = self._scope(value)
        digest, _, result = self._plan(scope)
        payload = {
            "id": secrets.token_hex(16),
            "scope": scope,
            "fingerprint": digest,
            "created": time.time(),
        }
        body = base64.urlsafe_b64encode(encode(payload).encode()).decode()
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return {**result, "preview_token": body + "." + signature, "expires_in": 900}

    def _decode(self, token):
        try:
            body, signature = token.split(".")
            expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            return json.loads(base64.urlsafe_b64decode(body))
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise ValueError("预览凭证无效，请重新预览") from None

    def execute(self, token):
        payload = self._decode(token)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            saved = self.db.execute(
                "SELECT result FROM usage_backfill_batches WHERE id=?", (payload["id"],)
            ).fetchone()
            if saved:
                return {**json.loads(saved[0]), "already_applied": True}
            now = time.time()
            if not 0 <= now - payload["created"] <= 900:
                raise StalePreview("预览已过期，请重新预览")
            digest, ready, result = self._plan(payload["scope"])
            if not hmac.compare_digest(digest, payload["fingerprint"]):
                raise StalePreview("相关记录或规则已变化，请重新预览后执行")
            for item in ready:
                row = item["row"]
                selection = {
                    **item["selection"],
                    "backfill": {
                        "batch_id": payload["id"],
                        "at": now,
                        "label": "按补录规则估算",
                        "rule_created": result["rate"]["created"],
                        "source_provider": row["provider"],
                        "rate_provider": result["rate"]["provider"],
                    },
                }
                before = {k: row[k] for k in BILLING_FIELDS}
                after = {
                    "rate_id": payload["scope"]["rate_id"],
                    "pricing_snapshot": encode(selection),
                    "tariff": selection["tariff"],
                    "cost": item["cost"],
                    "currency": item["currency"],
                    "cost_state": "estimated",
                    "updated": now,
                }
                self.db.execute(
                    "UPDATE usage_calls SET rate_id=?,pricing_snapshot=?,tariff=?,cost=?,"
                    "currency=?,"
                    "cost_state=?,updated=? WHERE id=?",
                    (*after.values(), row["id"]),
                )
                self.db.execute(
                    "INSERT INTO usage_backfill_items VALUES(?,?,?,?)",
                    (payload["id"], row["id"], encode(before), encode(after)),
                )
            result.pop("samples")
            result.update(
                {
                    "id": payload["id"],
                    "created": payload["created"],
                    "executed": now,
                    "label": "按补录规则估算",
                    "updated": len(ready),
                    "already_applied": False,
                }
            )
            self.db.execute(
                "INSERT INTO usage_backfill_batches VALUES(?,?,?,?,?)",
                (payload["id"], payload["created"], now, encode(payload["scope"]), encode(result)),
            )
        return result
