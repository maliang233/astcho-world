"""World capability discovery and execution, independent of the agent driver."""

from pathlib import Path


def function(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


class WorldTools:
    def __init__(self, kernel, *, skills: Path | None = None):
        self.kernel, self.skills = kernel, skills

    def tools(self):
        tools = [
            function("world_apps", "列出可用应用", {}),
            function("world_tools", "展开应用工具", {"app_id": {"type": "string"}}, ("app_id",)),
            function(
                "world_notifications",
                "默认只领取尚未送达的新通知，已在唤醒中收到的不会重复返回。"
                "需要回看已送达旧通知时，显式设置 include_delivered=true；不改变QQ消息已读状态。",
                {
                    "include_delivered": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            ),
            function("world_ignore", "忽略已送达通知", {"id": {"type": "string"}}, ("id",)),
            function(
                "world_open", "打开已送达通知，读取其上下文", {"id": {"type": "string"}}, ("id",)
            ),
            function(
                "world_remind",
                "设置稍后提醒",
                {"due": {"type": "number"}, "text": {"type": "string"}},
                ("due", "text"),
            ),
            function("world_skills", "列出可选使用知识", {}),
            function(
                "world_read_skill", "按需读取使用知识", {"name": {"type": "string"}}, ("name",)
            ),
            function(
                "world_recall",
                "回看自己已收到的观察与实际行动；列表为预览，event_seq 可读取原记录。"
                "不会读取未读应用存档。",
                {
                    "query": {"type": "string"},
                    "before": {"type": "integer", "minimum": 1},
                    "event_seq": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "activity_id": {"type": "string"},
                    "full": {"type": "boolean"},
                    "app": {"type": "string"},
                    "since": {"type": "number"},
                    "until": {"type": "number"},
                },
            ),
            function(
                "world_activity",
                "查看当前持续活动；可留下继续意图和已观察的资源引用",
                {
                    "resume_note": {"type": "string"},
                    "resource_refs": {"type": "array", "items": {"type": "string"}},
                },
            ),
            function(
                "world_pause",
                "暂停计算，等待明确 App 范围的新事件或一个时间点；"
                "事件等不到会静默放下，不设提醒、不循环等待。",
                {
                    "resume_note": {"type": "string"},
                    "expires_in": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
                    "app_id": {"type": "string"},
                    "scope": {"type": "string"},
                    "after_seconds": {"type": "number", "exclusiveMinimum": 0},
                },
                ("resume_note", "expires_in"),
            ),
            function(
                "world_finish",
                "结束当前活动。可自愿用 note 留一句自己认为值得记住的信息；"
                "不要求每次总结，没有就留空。环境只生成本地事实索引，不再另调用摘要模型。",
                {"note": {"type": "string"}},
            ),
        ]
        for app_id in self.kernel.store.get("expanded_apps", []):
            if app_id not in self.kernel.apps:
                continue
            for tool in self.kernel.apps[app_id].manifest.tools:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{app_id}__{tool.name}",
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    }
                )
        return tools

    async def execute(self, name, args, call_id):
        if name == "world_apps":
            return {"apps": self.kernel.catalog()}
        if name == "world_tools":
            self.kernel.apps[args["app_id"]]  # Validate before retaining.
            expanded = set(self.kernel.store.get("expanded_apps", []))
            expanded.add(args["app_id"])
            self.kernel.store.set("expanded_apps", sorted(expanded))
            return {
                "tools": [t.model_dump() for t in self.kernel.apps[args["app_id"]].manifest.tools]
            }
        if name == "world_notifications":
            return {"notifications": self.kernel.store.notifications(**args)}
        if name == "world_ignore":
            self.kernel.store.ignore(args["id"])
            return {"ignored": args["id"]}
        if name == "world_open":
            return (await self.kernel.open_notification(args["id"])).model_dump()
        if name == "world_remind":
            return {"id": self.kernel.store.remind(args["due"], args["text"], reminder_id=call_id)}
        if name == "world_skills":
            return {
                "skills": sorted(p.name for p in self.skills.glob("*.md")) if self.skills else []
            }
        if name == "world_read_skill":
            if self.skills is None:
                raise ValueError("No skills configured")
            path = (self.skills / args["name"]).resolve()
            if path.parent != self.skills.resolve() or path.suffix != ".md":
                raise ValueError("Invalid skill path")
            result = {"name": path.name, "text": path.read_text()}
            self.kernel.store.observe("skill", call_id, result)
            return result
        if name == "world_recall":
            from astcho_world.experience import recall

            if args.get("activity_id") and args.get("full"):
                result = self.kernel.activities.recall(args["activity_id"], full=True)
                self.kernel.store.observe("activity_archive", call_id, result)
                return result
            return recall(self.kernel.store, **args)
        if name == "world_activity":
            return {
                "activity": self.kernel.activities.update(**args)
                if args
                else self.kernel.activities.current()
            }
        if name == "world_pause":
            if args.get("app_id") and args["app_id"] not in self.kernel.apps:
                raise ValueError("Unknown application")
            return {"status": "waiting", "activity": self.kernel.activities.pause(**args)}
        if name == "world_finish":
            return {"status": "idle"}
        if "__" in name:
            app_id, tool = name.split("__", 1)
            allowed = {t["function"]["name"] for t in self.tools()}
            if name not in allowed:
                raise ValueError("Discover tools before using them")
            return (await self.kernel.call(app_id, tool, args, operation_id=call_id)).model_dump()
        raise ValueError("Unknown tool")
