"""Read-only view of legacy schedule/configuration and app-published cognition state."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from astcho.services.schedule import ScheduleService, _in_range
from astcho_world.presentation import KEY, schedule_editable, schedule_revision


def display_metadata(block):
    value = block.get("presentation")
    base = value.get("base") if isinstance(value, dict) else None
    return (
        {"base": base}
        if isinstance(base, str) and KEY.fullmatch(base) and base.startswith("base.")
        else {}
    )


class LifeState:
    def __init__(
        self, *, schedule=None, schedule_path=None, profile_path=None, demo=False, transport=None
    ):
        self.demo, self.transport = demo, transport
        self.schedule = schedule or ScheduleService(
            Path(
                schedule_path
                or (None if demo else os.getenv("ASTCHO_SCHEDULE_PATH"))
                or "/__astcho_unconfigured_schedule__"
            )
        )
        self.profile_path = Path(
            profile_path
            or (None if demo else os.getenv("ASTCHO_PROFILE_PATH"))
            or "/__astcho_unconfigured_profile__"
        )

    def snapshot(self, kernel):
        now = datetime.now()
        current = self.schedule.current(now)
        blocks, routine, append = self.schedule._resolve_blocks(now, now.strftime("%A").lower())
        active = next(
            (
                i
                for i, block in enumerate(blocks)
                if _in_range(now.strftime("%H:%M"), str(block["start"]), str(block["end"]))
            ),
            None,
        )
        configured = self.schedule.path.is_file()
        display = display_metadata(blocks[active]) if active is not None else {}
        display_base = (
            display.get("base", "base.idle") if isinstance(display, dict) else "base.idle"
        )
        profile = {}
        if self.profile_path.is_file():
            try:
                profile = json.loads(self.profile_path.read_text())
                if not isinstance(profile, dict):
                    profile = {}
            except (OSError, ValueError):
                pass
        report = {}
        plugin = kernel.apps.get("qq")
        if plugin:
            path = plugin.data_dir / "dashboard-state.json"
            try:
                report = json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        data = report.get("data", {}).get("cognition", {})
        running = bool(plugin and plugin.process and plugin.process.returncode is None)
        initialized = running and bool(data.get("initialized"))
        memory_enabled = (
            None
            if self.demo
            else os.getenv("ASTCHO_MEMORY_ENABLED", "true").lower()
            in {
                "1",
                "true",
                "yes",
                "on",
            }
        )
        connection = (
            kernel.store.get(
                "qq_connection", {"state": "disconnected", "transport": self.transport}
            )
            if (self.transport is not None)
            else {"state": "not_running", "transport": "reverse_ws"}
        )
        return {
            "schedule": {
                "configured": configured,
                "source": self.schedule.path.name if configured else None,
                "date": now.strftime("%Y-%m-%d"),
                "timezone": str(now.astimezone().tzinfo),
                "current": asdict(current),
                "presentation": {
                    "base": "base.idle" if current.overridden else display_base,
                    "configured": not current.overridden
                    and isinstance(display, dict)
                    and "base" in display,
                },
                "presentation_revision": schedule_revision(self.schedule._config),
                "presentation_editable": schedule_editable(kernel, self),
                "presentation_blocks": [
                    {
                        "routine": name,
                        "index": i,
                        "start": str(b["start"]),
                        "end": str(b["end"]),
                        "base": display_metadata(b).get("base", "base.idle"),
                        "configured": "base" in display_metadata(b),
                    }
                    for name, values in self.schedule._config.get(
                        "routines", self.schedule._config
                    ).items()
                    if isinstance(values, list)
                    for i, b in enumerate(values)
                ],
                "today": {
                    "routine": routine,
                    "mood_append": append,
                    "blocks": [
                        {
                            "start": str(block["start"]),
                            "end": str(block["end"]),
                            "talk_value": int(block.get("talk_value", 50)),
                            "mood": str(block.get("mood", block.get("mood_prompt", "calm"))),
                            "active": not current.overridden and i == active,
                        }
                        for i, block in enumerate(blocks)
                    ],
                },
                "used_by": ["QQ 通知概率", "原聊天日程上下文"],
            },
            "profile": {
                "configured": bool(profile),
                "name": profile.get("name", "星回"),
                "source": self.profile_path.name if profile else None,
            },
            "connection": connection,
            "cognition": {
                "initialized": initialized,
                "last_report": report.get("updated"),
                "memory_enabled": data.get("memory_enabled", memory_enabled),
                "memory_count": data.get("memory_count") if initialized else None,
                "moods": data.get("moods", []) if initialized else [],
            },
            "components": [
                {
                    "name": "日程",
                    "status": "已接入" if configured else "使用默认值",
                    "detail": "沿用原日程规则，作用于通知概率和原聊天上下文。",
                },
                {
                    "name": "人格",
                    "status": "已配置" if profile else "未配置",
                    "detail": "原聊天按需载入；原有 prompt 保持原样。",
                },
                {
                    "name": "情绪与关系",
                    "status": "已初始化" if initialized else "按需初始化",
                    "detail": "复用原聊天服务；没有实际状态时不显示推测数值。",
                },
                {
                    "name": "长期记忆",
                    "status": "已关闭"
                    if memory_enabled is False
                    else ("已初始化" if initialized else "按需初始化"),
                    "detail": "原记忆链路按需使用，新环境数据库独立；旧数据未迁移。",
                },
                {
                    "name": "媒体与后台学习",
                    "status": "尚未完整接入",
                    "detail": "媒体理解、表达/表情学习和习俗跟风仍需完成观察边界适配。",
                },
            ],
        }
