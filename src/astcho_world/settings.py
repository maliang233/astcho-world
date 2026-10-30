"""Categorized administrative configuration, with an explicit safe field allowlist."""

import os

from astcho_world.pricing import presets
from astcho_world.usage import provider_id
from astcho_world.usage_backfill import history


def settings_snapshot(kernel, mode, *, life=None):
    from astcho_world import presentation

    life_view = life.snapshot(kernel) if life else None

    def value(name):
        return os.getenv(name) if mode != "demo" else None

    model_rows = []
    for title, model_key, url_key, key_name in (
        ("环境主体", "ASTCHO_WORLD_MODEL", "ASTCHO_WORLD_BASE_URL", "ASTCHO_WORLD_API_KEY"),
        (
            "普通私聊及其他文本处理",
            "ASTCHO_CHAT_MODEL",
            "ASTCHO_LLM_BASE_URL",
            "ASTCHO_LLM_API_KEY",
        ),
        ("识图", "ASTCHO_VISION_MODEL", "ASTCHO_VISION_BASE_URL", "ASTCHO_VISION_API_KEY"),
    ):
        model_rows.append(
            {
                "name": title,
                "model": value(model_key),
                "provider": provider_id(value(url_key)) if value(url_key) else None,
                "credential_configured": bool(value(key_name)),
                "editable": False,
                "source": ".env / 启动参数",
            }
        )
    options = {
        key: [
            r[0]
            for r in kernel.store.db.execute(
                f"SELECT DISTINCT {key} FROM usage_calls ORDER BY {key}"
            )
        ]
        for key in ("provider", "model")
    }
    return {
        "categories": [
            {"id": "billing", "name": "计费"},
            {"id": "models", "name": "模型与连接"},
            {"id": "apps", "name": "应用"},
            {"id": "interface", "name": "界面与运行"},
            {"id": "presentation", "name": "立绘与状态"},
        ],
        "billing": {
            "rates": kernel.store.usage.rates(),
            "backfills": history(kernel.store.db),
            "presets": presets(),
            "options": options,
        },
        "models": model_rows,
        "presentation": presentation.settings(kernel, life_view, mode),
        "apps": [{**item, "editable": False} for item in kernel.catalog()],
        "interface": {
            "timezone": "Asia/Shanghai",
            "mode": mode,
            "portrait": "/assets/astcho-desk.png",
            "portrait_style": "桌前上半身 · 五种姿态，按基础状态与活动切换",
            "debug_default": "默认收起",
            "editable": False,
        },
        "runtime": {
            "phase": kernel.store.get("runtime_status", {}).get("phase"),
            "runner": kernel.store.get("runtime_status", {}).get("runner"),
            "configuration": ".env / 应用清单 / 启动参数",
            "restart_required": "模型与接入配置修改后需要重启；计费规则保存后即时生效",
        },
    }
