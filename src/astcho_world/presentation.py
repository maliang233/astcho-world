"""Administrator-only display projection. Never decides, wakes, reads an App or prompts a model."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import struct
import time
import zlib
from pathlib import Path

KEY = re.compile(r"^(base|activity)\.[a-z][a-z0-9_.-]{0,79}$")
ASSETS = Path(__file__).with_name("web")
BUILTINS = {
    "idle": ("安静待着", "astcho-desk.png"),
    "school": ("上课 / 写笔记", "astcho-school-v1.png"),
    "thinking": ("思考", "astcho-thinking-v1.png"),
    "sleep": ("睡眠", "astcho-sleep-v2.png"),
    "phone": ("看手机", "astcho-phone-v1.png"),
}
NATIVE_ACTIVITIES = {
    "web_search": {
        "kind": "activity.phone",
        "label": "正在搜索网页",
        "fallback": "base.idle",
        "hold_seconds": 20,
    },
    "web_fetch": {
        "kind": "activity.phone",
        "label": "正在阅读网页",
        "fallback": "base.idle",
        "hold_seconds": 20,
    },
}
DEFAULTS = {
    "base.idle": {"label": "安静待着", "asset": "idle", "fallback": None},
    "base.class": {"label": "上课", "asset": "school", "fallback": "base.idle"},
    "base.sleep": {"label": "睡眠", "asset": "sleep", "fallback": "base.idle"},
    "activity.thinking": {"label": "正在思考", "asset": "thinking", "fallback": "base.idle"},
    "activity.phone": {"label": "正在看手机", "asset": "phone", "fallback": "base.idle"},
    "activity.notes": {"label": "正在写笔记", "asset": "school", "fallback": "base.idle"},
    "activity.general": {"label": "正在使用应用", "asset": None, "fallback": "base.idle"},
}


def asset_path(kernel, asset):
    if isinstance(asset, str) and asset in BUILTINS:
        return ASSETS / BUILTINS[asset][1]
    if isinstance(asset, str) and re.fullmatch(r"upload-[0-9a-f]{64}", asset):
        return kernel.root / "presentation" / (asset + ".png")
    return None


def config(kernel):
    return kernel.store.get("presentation_config", {"revision": 0, "states": {}, "tools": {}})


def registry(kernel, *, include_saved=True):
    states = copy.deepcopy(DEFAULTS)
    for app in kernel.apps.values():
        if app.manifest.dashboard:
            for signal in app.manifest.dashboard.activities.values():
                states.setdefault(
                    signal.kind, {"label": signal.label, "asset": None, "fallback": signal.fallback}
                )
                states.setdefault(
                    signal.fallback,
                    {"label": signal.fallback, "asset": None, "fallback": "base.idle"},
                )
    if include_saved:
        states.update(config(kernel)["states"])
    return states


def resolve_asset(kernel, kind, states=None):
    states = states or registry(kernel)
    chain, seen = [], set()
    current = kind
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        row = states.get(current, {})
        asset = row.get("asset")
        path = asset_path(kernel, asset)
        if path and path.is_file():
            return {
                "asset": asset,
                "url": "/api/presentation/assets/" + asset,
                "chain": chain,
                "fallback": current != kind,
            }
        current = row.get("fallback", "base.idle")
    return {
        "asset": "idle",
        "url": "/assets/astcho-desk.png",
        "chain": chain + ["base.idle"],
        "fallback": True,
    }


def save_config(kernel, value):
    if (
        not isinstance(value, dict)
        or type(value.get("revision")) is not int
        or value.get("revision") != config(kernel)["revision"]
    ):
        raise ValueError("配置已变化，请刷新后重试")
    changes = value.get("states")
    if not isinstance(changes, dict) or len(changes) > 200:
        raise ValueError("状态映射格式不正确，最多 200 项")
    clean = {}
    for key, row in changes.items():
        if not KEY.fullmatch(key) or not isinstance(row, dict):
            raise ValueError("状态标识必须以 base. 或 activity. 开头")
        label, asset, fallback = row.get("label"), row.get("asset"), row.get("fallback")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
            raise ValueError("请填写状态名称（1 至 100 字）")
        if fallback is not None and (not isinstance(fallback, str) or not KEY.fullmatch(fallback)):
            raise ValueError("无效的回退状态")
        if asset is not None:
            path = asset_path(kernel, asset)
            if not path or not path.is_file():
                raise ValueError("请选择已上传或内置的有效图片")
        clean[key] = {"label": label.strip(), "asset": asset, "fallback": fallback}
    merged = {**registry(kernel, include_saved=False), **clean}
    for key in merged:
        seen, current = set(), key
        while current:
            if current in seen:
                raise ValueError("回退关系不能成环")
            seen.add(current)
            row = merged.get(current)
            if row is None:
                raise ValueError("回退状态不存在，请先添加")
            current = row.get("fallback")
    if not merged["base.idle"].get("asset"):
        raise ValueError("安静待着需要保留一张默认图片")
    tools = value.get("tools", {})
    declared = {
        f"{a.manifest.app_id}__{t.name}"
        for a in kernel.apps.values()
        for t in a.manifest.tools
        if t.name not in a.manifest.admin_tools
    }
    declared.update("dsh__" + name for name in NATIVE_ACTIVITIES)
    if not isinstance(tools, dict) or any(
        k not in declared
        or not isinstance(v, str)
        or v not in merged
        or not v.startswith("activity.")
        for k, v in tools.items()
    ):
        raise ValueError("工具映射必须使用已注册的主体工具与活动状态")
    result = {"revision": config(kernel)["revision"] + 1, "states": clean, "tools": tools}
    kernel.store.set("presentation_config", result)
    return result


def upload(kernel, data):
    # PNG only: bounded size/dimensions, verified chunk CRCs, no SVG/scripts/paths.
    if len(data) > 8 * 1024 * 1024 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("请上传 8 MiB 以内的 PNG 图片")
    cursor, first, ended = 8, True, False
    while cursor + 12 <= len(data):
        length = struct.unpack(">I", data[cursor : cursor + 4])[0]
        kind = data[cursor + 4 : cursor + 8]
        body = data[cursor + 8 : cursor + 8 + length]
        end = cursor + 12 + length
        if (
            end > len(data)
            or zlib.crc32(kind + body) != struct.unpack(">I", data[end - 4 : end])[0]
        ):
            raise ValueError("PNG 文件不完整或校验失败")
        if first:
            if kind != b"IHDR" or length != 13:
                raise ValueError("无效的 PNG 头")
            width, height = struct.unpack(">II", body[:8])
            if not (1 <= width <= 4096 and 1 <= height <= 4096):
                raise ValueError("图片边长不能超过 4096 像素")
            first = False
        cursor = end
        if kind == b"IEND":
            ended = length == 0 and cursor == len(data)
            break
    if not ended:
        raise ValueError("PNG 文件缺少结束标记")
    asset = "upload-" + hashlib.sha256(data).hexdigest()
    path = asset_path(kernel, asset)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists() or path.read_bytes() != data:
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(path)
    return {"asset": asset, "url": "/api/presentation/assets/" + asset}


def tool_signal(kernel, name):
    if "__" not in name:
        return None
    app_id, tool = name.split("__", 1)
    app = kernel.apps.get(app_id)
    native = NATIVE_ACTIVITIES.get(tool) if app_id == "dsh" else None
    if not app and not native:
        return None
    override = config(kernel).get("tools", {}).get(name)
    if override:
        row = registry(kernel).get(override, {})
        return {
            "kind": override,
            "label": row.get("label", override),
            "fallback": row.get("fallback"),
            "hold_seconds": 20,
            "app_id": app_id,
            "tool": tool,
            "configured_by": "settings",
        }
    if native:
        return {**native, "app_id": app_id, "tool": tool, "configured_by": "native"}
    dashboard = app.manifest.dashboard if app else None
    signal = dashboard.activities.get(tool) if dashboard else None
    return (
        {**signal.model_dump(), "app_id": app_id, "tool": tool, "configured_by": "manifest"}
        if signal
        else {
            "kind": "activity.general",
            "label": "正在使用 " + (dashboard.name if dashboard else app_id),
            "fallback": "base.idle",
            "hold_seconds": 0,
            "app_id": app_id,
            "tool": tool,
            "configured_by": "generic",
        }
    )


def current_activity(kernel, runtime, now):
    if runtime.get("phase") not in {"thinking", "executing"}:
        return None
    # Only current-run, explicit tool actions. Notifications and admin tools never qualify.
    boundary = kernel.store.db.execute(
        "SELECT seq,activity_id FROM events WHERE kind='activity.started' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if not boundary:
        return None
    rows = kernel.store.db.execute(
        "SELECT e.*,p.tool,p.status,p.app AS action_app FROM events e "
        "JOIN operations p ON p.id=e.operation_id "
        "WHERE e.kind IN ('operation.started','decision.tool') AND e.seq>? AND e.activity_id=? "
        "AND e.created>=? ORDER BY e.seq DESC LIMIT 30",
        (boundary[0], boundary[1], now - 120),
    ).fetchall()
    for row in rows:
        signal = tool_signal(kernel, f"{row['action_app']}__{row['tool']}")
        app = kernel.apps.get(row["action_app"])
        if not signal or (app and row["tool"] in app.manifest.admin_tools):
            continue
        if row["status"] == "running":
            return {
                **signal,
                "source": "operation.running",
                "event_seq": row["seq"],
                "operation_id": row["operation_id"],
                "updated": row["created"],
            }
        settled = kernel.store.db.execute(
            "SELECT created FROM events WHERE kind IN ('operation.finished','decision.result') "
            "AND operation_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        if row["status"] == "succeeded" and settled and now - settled[0] <= signal["hold_seconds"]:
            return {
                **signal,
                "source": "operation.recent",
                "event_seq": row["seq"],
                "operation_id": row["operation_id"],
                "updated": settled[0],
                "expires_at": settled[0] + signal["hold_seconds"],
            }
        # A newer specific action supersedes older ones, even if it failed/expired.
        return None
    return None


def snapshot(kernel, life, mode, *, now=None):
    now = time.time() if now is None else now
    schedule = (life or {}).get("schedule", {})
    base = schedule.get("presentation", {}).get("base", "base.idle")
    states = registry(kernel)
    if not isinstance(base, str) or not KEY.fullmatch(base) or not base.startswith("base."):
        base = "base.idle"
    states.setdefault(base, {"label": base, "asset": None, "fallback": "base.idle"})
    runtime = kernel.store.get("runtime_status", {})
    available = mode in {"live", "demo"} and runtime.get("phase") not in {"offline", "observer"}
    action = current_activity(kernel, runtime, now) if available else None
    if not action and available and runtime.get("phase") == "thinking":
        action = {
            "kind": "activity.thinking",
            "label": "正在思考下一步",
            "source": "runtime",
            "updated": runtime.get("updated"),
        }
    kind = action["kind"] if action else base if available else "base.idle"
    chosen = resolve_asset(kernel, kind, states)
    return {
        "base": {
            "kind": base,
            "label": states[base]["label"],
            "source": "schedule.presentation",
            "configured": schedule.get("presentation", {}).get("configured", False),
        },
        "activity": action,
        "selected": kind,
        "label": action["label"] if action else states[kind]["label"],
        "image": chosen,
        "available": available,
        "revision": config(kernel)["revision"],
        "note": "立绘仅展示实际活动与日程，不代表推测情绪",
    }


def settings(kernel, life, mode):
    states = registry(kernel)
    schedule = (life or {}).get("schedule", {})
    schedule_rows = schedule.get("presentation_blocks", [])
    for row in schedule_rows:
        states.setdefault(
            row["base"], {"label": row["base"], "asset": None, "fallback": "base.idle"}
        )
    return {
        "revision": config(kernel)["revision"],
        "states": states,
        "tool_overrides": config(kernel).get("tools", {}),
        "assets": [
            {"id": key, "label": label, "url": "/api/presentation/assets/" + key}
            for key, (label, name) in BUILTINS.items()
            if (ASSETS / name).is_file()
        ]
        + [
            {
                "id": p.stem,
                "label": "上传 · " + p.stem[7:19],
                "url": "/api/presentation/assets/" + p.stem,
            }
            for p in sorted((kernel.root / "presentation").glob("upload-*.png"))
        ],
        "resolved": {k: resolve_asset(kernel, k, states) for k in states},
        "current": snapshot(kernel, life, mode),
        "schedule": schedule_rows,
        "schedule_revision": schedule.get("presentation_revision"),
        "schedule_editable": schedule.get("presentation_editable", False),
        "tools": [
            {
                "app": a.manifest.app_id,
                "tool": t.name,
                "signal": tool_signal(kernel, f"{a.manifest.app_id}__{t.name}"),
            }
            for a in kernel.apps.values()
            for t in a.manifest.tools
            if t.name not in a.manifest.admin_tools
        ]
        + [
            {"app": "dsh", "tool": name, "signal": tool_signal(kernel, "dsh__" + name)}
            for name in NATIVE_ACTIVITIES
        ],
    }


def schedule_revision(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def schedule_editable(kernel, life):
    path = life.schedule.path.resolve() if life else None
    project = Path(__file__).resolve().parents[2]
    return bool(
        path
        and path.is_file()
        and (path.is_relative_to(project) or path.is_relative_to(kernel.root.resolve()))
    )


def save_schedule(kernel, life, value):
    if not schedule_editable(kernel, life):
        raise ValueError("此日程不在当前项目或运行目录内，仅允许查看")
    if not isinstance(value, dict) or not isinstance(value.get("blocks"), list):
        raise ValueError("日程显示映射格式不正确")
    path = life.schedule.path
    raw = json.loads(path.read_text())
    if value.get("revision") != schedule_revision(raw):
        raise ValueError("日程已变化，请刷新后重新分类")
    groups = raw.get("routines", raw)
    states = registry(kernel)
    for item in value["blocks"]:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise ValueError("无效的日程时段")
        name, i, base = item.get("routine"), item["index"], item.get("base")
        blocks = groups.get(name) if isinstance(name, str) else None
        if (
            not isinstance(blocks, list)
            or not 0 <= i < len(blocks)
            or not isinstance(base, str)
            or not base.startswith("base.")
            or base not in states
        ):
            raise ValueError("请选择存在的日程时段与基础状态")
        previous = blocks[i].get("presentation")
        blocks[i]["presentation"] = {
            **(previous if isinstance(previous, dict) else {}),
            "base": base,
        }
    # Preserve original text as a versioned backup. Only display metadata is edited.
    backup = path.with_name(path.name + ".portrait-" + value["revision"][:12] + ".bak")
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".portrait-tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    life.schedule.reload()
    kernel.store.set("presentation_schedule_revision", time.time())
    return {"revision": schedule_revision(raw)}
