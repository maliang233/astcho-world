"""Versioned prices: request-start clocks and total-input whole-request tiers."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRICE_KEYS = ("input_price", "cached_price", "output_price")
PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
HOLIDAY_SOURCE = "https://www.gov.cn/zhengce/content/202511/content_7047090.htm"


def prices(value):
    if not isinstance(value, dict):
        raise ValueError("请填写输入、缓存命中和输出单价")
    result = {}
    for key in PRICE_KEYS:
        item = value.get(key)
        if key == "cached_price" and (item is None or item == ""):
            result[key] = None
            continue
        try:
            number = Decimal(str(item))
            if not number.is_finite() or not 0 <= number <= 1_000_000_000:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("单价应为有限非负数，单位为每百万 token") from None
        result[key] = str(number)
    return result


def validate_tiers(value, *, timed=False):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("basis", "input_tokens") != "input_tokens"
        or value.get("method", "whole_request") != "whole_request"
    ):
        raise ValueError("仅支持按单次总输入 token 选档，整次请求采用该档价格")
    rows = value.get("rows")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("请配置 1 至 16 个 token 档位")
    clean, previous = [], 0
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("token 档位格式不正确")
        upper = row.get("up_to")
        if upper is None:
            if i != len(rows) - 1:
                raise ValueError("只有最后一档可不设上限")
        elif type(upper) is not int or not previous < upper <= 1_000_000_000:
            raise ValueError("token 上限必须为递增正整数，使用实际数量而非 K/M")
        item = {"up_to": upper, **prices(row)}
        if timed:
            item["peak"] = prices(row.get("peak"))
        clean.append(item)
        previous = upper
    return {
        "basis": "input_tokens",
        "method": "whole_request",
        "rows": clean,
        "source": str(value.get("source", "用户配置"))[:500],
        "verified_at": str(value.get("verified_at", ""))[:30],
        "note": str(value.get("note", ""))[:500],
    }


def minute(text, *, end=False):
    if end and text == "24:00":
        return 1440
    try:
        value = datetime.strptime(text, "%H:%M")
        if value.strftime("%H:%M") != text:
            raise ValueError
        return value.hour * 60 + value.minute
    except (ValueError, TypeError):
        raise ValueError("时段应使用 HH:MM，结束时刻可为 24:00") from None


def validate_scheme(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("kind") != "time_of_use":
        raise ValueError("未知计价方式")
    timezone = value.get("timezone", "Asia/Shanghai")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("无效的计费时区") from None
    windows = value.get("windows")
    if not isinstance(windows, list) or not 1 <= len(windows) <= 16:
        raise ValueError("请配置 1 至 16 个高峰时段")
    occupied = set()
    clean = []
    for row in windows:
        if not isinstance(row, dict):
            raise ValueError("高峰时段格式不正确")
        days = row.get("days")
        if (
            not isinstance(days, list)
            or not days
            or len(set(days)) != len(days)
            or any(type(d) is not int or not 0 <= d <= 6 for d in days)
        ):
            raise ValueError("星期应为不重复的 0（周一）至 6（周日）")
        start, end = minute(row.get("start")), minute(row.get("end"), end=True)
        if start == end:
            raise ValueError("时段起止不能相同，全天请使用 00:00–24:00")
        span = (end - start) if end > start else 1440 - start + end
        for day in days:
            cells = {(day * 1440 + start + n) % 10080 for n in range(span)}
            if occupied & cells:
                raise ValueError("高峰时段不能重叠（包括跨午夜与跨周时段）")
            occupied |= cells
        clean.append({"days": days, "start": row["start"], "end": row["end"]})
    calendar = value.get("calendar")
    if calendar is not None:
        if not isinstance(calendar, dict):
            raise ValueError("节假日日历格式不正确")
        years, dates = calendar.get("years"), calendar.get("off_peak_dates")
        if (
            not isinstance(years, list)
            or not years
            or any(type(y) is not int or not 2000 <= y <= 2100 for y in years)
            or not isinstance(dates, list)
            or len(dates) > 1000
        ):
            raise ValueError("日历必须注明已核对的年份与空闲日期")
        for item in dates:
            try:
                parsed = date.fromisoformat(item)
                if parsed.isoformat() != item or parsed.year not in years:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("节假日日期必须为 YYYY-MM-DD，且属于已核对年份") from None
        calendar = {
            "years": sorted(set(years)),
            "off_peak_dates": sorted(set(dates)),
            "version": str(calendar.get("version", "custom"))[:100],
            "source": str(calendar.get("source", "用户配置"))[:500],
        }
    return {
        "kind": "time_of_use",
        "timezone": timezone,
        "windows": clean,
        "peak": prices(value.get("peak")),
        "off_peak": prices(value.get("off_peak")),
        "calendar": calendar,
        "source": str(value.get("source", "用户配置"))[:500],
        "verified_at": str(value.get("verified_at", ""))[:30],
        "time_basis": "local_request_start",
    }


def select_tariff(rate, timestamp):
    """Return the complete immutable selection, including why a tariff was used."""
    scheme = json.loads(rate["scheme"]) if rate.get("scheme") else None
    base = {
        "rate_id": rate["id"],
        "currency": rate["currency"],
        "rule": scheme,
        "time_basis": "local_request_start",
    }
    if rate.get("tiers"):
        base["tiers"] = json.loads(rate["tiers"])
    if scheme is None:
        return {
            **base,
            **{key: rate[key] for key in PRICE_KEYS},
            "tariff": "tiered" if base.get("tiers") else "fixed",
            "reason": "按单次输入 token 选档" if base.get("tiers") else "固定单价",
        }
    local = datetime.fromtimestamp(timestamp, ZoneInfo(scheme["timezone"]))
    base.update({"timezone": scheme["timezone"], "local_time": local.isoformat()})
    day = local.date().isoformat()
    calendar = scheme.get("calendar")
    # Be conservative outside the calendar's verified coverage, even on weekends.
    if calendar and local.year not in calendar["years"]:
        return {**base, "tariff": "unknown", "reason": "该年份的节假日日历尚未核对"}
    if calendar and day in calendar["off_peak_dates"]:
        return {**base, **scheme["off_peak"], "tariff": "off_peak", "reason": "日历空闲日期"}
    clock = local.hour * 60 + local.minute
    for window in scheme["windows"]:
        start, end = minute(window["start"]), minute(window["end"], end=True)
        if end > start:
            match = local.weekday() in window["days"] and start <= clock < end
        else:
            match = (local.weekday() in window["days"] and clock >= start) or (
                (local.weekday() - 1) % 7 in window["days"] and clock < end
            )
        if match:
            return {**base, **scheme["peak"], "tariff": "peak", "reason": "命中高峰时段"}
    return {**base, **scheme["off_peak"], "tariff": "off_peak", "reason": "不在高峰时段"}


def resolve_tier(selection, input_tokens):
    """Resolve against the frozen rule, never against the current rate table."""
    if not selection or not selection.get("tiers"):
        return selection
    result = {k: v for k, v in selection.items() if k not in PRICE_KEYS}
    result["tier"] = {
        "input_tokens": input_tokens,
        "basis": "input_tokens_including_cache",
        "method": "whole_request",
        "state": "missing_input" if input_tokens is None else "out_of_range",
    }
    lower = 0
    for row in selection["tiers"]["rows"]:
        upper = row["up_to"]
        if input_tokens is not None and (upper is None or input_tokens <= upper):
            result["tier"].update(state="matched", lower_exclusive=lower, up_to=upper)
            if selection["tariff"] != "unknown":
                result.update(row["peak"] if selection["tariff"] == "peak" else prices(row))
            break
        lower = upper
    return result


def china_calendar_2026():
    days = []
    for start, end in (
        ("01-01", "01-03"),
        ("02-15", "02-23"),
        ("04-04", "04-06"),
        ("05-01", "05-05"),
        ("06-19", "06-21"),
        ("09-25", "09-27"),
        ("10-01", "10-07"),
    ):
        current, last = date.fromisoformat("2026-" + start), date.fromisoformat("2026-" + end)
        while current <= last:
            days.append(current.isoformat())
            current += timedelta(days=1)
    return {
        "years": [2026],
        "off_peak_dates": days,
        "source": HOLIDAY_SOURCE,
        "version": "cn-public-holidays-2026-gbfmd-2025-7",
    }


def presets():
    result = []
    for model, off, peak in (
        ("deepseek-flash", ("1", "0.02", "4"), ("2", "0.04", "8")),
        ("deepseek-v4-pro", ("4.5", "0.15", "13.5"), ("9", "0.30", "27")),
    ):
        scheme = {
            "kind": "time_of_use",
            "timezone": "Asia/Shanghai",
            "windows": [
                {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "12:00"},
                {"days": [0, 1, 2, 3, 4], "start": "14:00", "end": "18:00"},
            ],
            "peak": dict(zip(PRICE_KEYS, peak, strict=True)),
            "off_peak": dict(zip(PRICE_KEYS, off, strict=True)),
            "calendar": china_calendar_2026(),
            "source": PRICE_SOURCE,
            "verified_at": "2026-10-28",
        }
        result.append(
            {
                "id": model,
                "name": f"DeepSeek · {model} · 2026-10-28",
                "provider": "api.deepseek.com",
                "model": model,
                "currency": "CNY",
                "scheme": validate_scheme(scheme),
            }
        )
    result.extend(qwen_presets())
    return result


def qwen_presets():
    result = []
    for model, rows in (
        ("qwen3.7-flash", [(32000, "0.2", "0.8"), (256000, "0.6", "2.4"), (1000000, "1.2", "4.8")]),
        ("qwen3.7-plus", [(256000, "2", "8"), (1000000, "6", "24")]),
    ):
        tiers = validate_tiers(
            {
                "rows": [
                    {
                        "up_to": upper,
                        "input_price": normal,
                        "cached_price": str(Decimal(normal) * Decimal("0.2")),
                        "output_price": output,
                    }
                    for upper, normal, output in rows
                ],
                "source": "https://help.aliyun.com/zh/model-studio/model-pricing",
                "verified_at": "2026-10-28",
                "note": "华北2（北京）标准价，隐式缓存命中按输入价20%；不含限时折扣、"
                "Batch及显式缓存创建/存储费用。其他地区或代理须修改。",
            }
        )
        result.append(
            {
                "id": "bailian-beijing-" + model,
                "name": f"百炼北京 · {model} · 标准价/隐式缓存 · 2026-10-28",
                "provider": "dashscope.aliyuncs.com/compatible-mode/v1",
                "model": model,
                "currency": "CNY",
                "scheme": None,
                "tiers": tiers,
                **prices(tiers["rows"][0]),
            }
        )
    return result


def estimate_cost(selection, input_tokens, output_tokens, cached_tokens):
    """One calculator for live settlement and explicit historical estimates."""
    if input_tokens is None or output_tokens is None:
        return None, None, "unknown_usage"
    if selection is None:
        return None, None, "unpriced"
    selection = resolve_tier(selection, input_tokens)
    currency = selection["currency"]
    if selection["tariff"] == "unknown":
        return None, currency, "unknown_schedule"
    if selection.get("tier", {}).get("state", "matched") != "matched":
        return None, currency, "unknown_tier"
    normal = Decimal(selection["input_price"])
    cached = Decimal(selection["cached_price"]) if selection["cached_price"] is not None else normal
    if cached_tokens is None and normal != cached:
        return None, currency, "unknown_cache"
    hits = cached_tokens or 0
    cost = (
        (input_tokens - hits) * normal
        + hits * cached
        + output_tokens * Decimal(selection["output_price"])
    ) / 1_000_000
    return str(cost), currency, "estimated"
