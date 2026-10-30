"""Actual DeepSeek Harness Python SDK driver for World, with native Cordis tools."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from jsonschema import validate

from astcho_world.dsh_capabilities import capability_status, plugin_rows
from astcho_world.prompting import environment_prompt, runtime_context
from astcho_world.toolbox import WorldTools


@dataclass(frozen=True)
class DSHModelConfig:
    model: str
    api_key: str
    base_url: str
    context_window: int = 65536
    max_tokens: int = 8192

    def __post_init__(self):
        if not self.model or not self.api_key or not self.base_url:
            raise ValueError("DSH model, API key and base URL are required")
        if not 0 < self.max_tokens <= self.context_window:
            raise ValueError("DSH token limits must satisfy 0 < max_tokens <= context_window")


class DSHAgent:
    def __init__(
        self,
        kernel,
        model,
        *,
        skills=None,
        profile_path=None,
        context_provider=None,
        wake_policy=None,
        activity_context=False,
        recent_activities=10,
        workspace_path=None,
    ):
        self.kernel, self.model = kernel, model
        self.tools = WorldTools(kernel, skills=skills)
        self.activity_context = activity_context
        if recent_activities < 1:
            raise ValueError("recent_activities must be positive")
        self.recent_activities = recent_activities
        self.workspace_path = workspace_path
        self.skills = skills
        # Claim before the host ticker starts, so an overdue legacy reminder
        # cannot race its import into the native timer service.
        kernel.store.set("reminder_backend", "dsh")
        self.system_prompt = environment_prompt(profile_path)
        self.context_provider = context_provider
        self.wake_policy = wake_policy or {"mode": "single_run"}
        self.lock = asyncio.Lock()
        self.harness = self.session = self.server = self.directory = None
        self.bridge_ready = False
        self.active = None
        self.bridge_tasks = set()
        self.root = (kernel.root / "dsh").resolve()
        saved = kernel.store.get("dsh_session")
        self.session_state = saved or {"id": "astcho-" + uuid.uuid4().hex, "started": False}

    async def start(self):
        if self.harness is not None:
            return
        from deepseek_harness import DeepSeekHarness

        self.loop = asyncio.get_running_loop()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace = Path(self.workspace_path) if self.workspace_path else self.root / "workspace"
        workspace.mkdir(exist_ok=True, mode=0o700)
        self.directory = tempfile.TemporaryDirectory(prefix="astcho-dsh-", dir="/tmp")
        socket = str(Path(self.directory.name) / "world.sock")
        self.token = secrets.token_urlsafe(32)
        self.server = await asyncio.start_unix_server(
            self._bridge, path=socket, limit=16 * 1024 * 1024
        )
        os.chmod(socket, 0o600)
        patch = self.root / "world.patch.yml"
        rows = [
            {"id": name, "disabled": True}
            for name in (
                "persistent-bash",
                "persistent-pwsh",
                "tool-fs",
                "tool-working-directory",
                "session-log-deepseek",
                "plugin-package-inventory-deepseek",
                "deepseek-llm-api-extensions",
                "llm-deepseek",
            )
        ]
        # DSH's generic 65536-token summary headroom cannot fit our existing
        # 65536-token route. Reuse its existing output reservation; this is an
        # automatic compaction policy, not a new input cap or truncation rule.
        rows.append(
            {
                "id": "compaction-basic",
                "config": {
                    "headroomTokens": self.model.max_tokens,
                    "maxTokens": self.model.max_tokens,
                },
            }
        )
        rows.append(
            {
                "insert": [
                    *plugin_rows(self.root, self.skills),
                    {
                        "id": "world-model",
                        "name": "@deepseek-ai/dsh-llm-pi-ai",
                        "config": {
                            "providers": {
                                "astcho-world": {
                                    "apiKeyEnv": "ASTCHO_DSH_MODEL_KEY",
                                    "api": "openai-completions",
                                    "baseURL": self.model.base_url,
                                    "models": [
                                        {
                                            "id": self.model.model,
                                            "contextWindow": self.model.context_window,
                                            "maxTokens": self.model.max_tokens,
                                        }
                                    ],
                                    "retryPolicy": {"mode": "normal", "maxRetries": 0},
                                }
                            },
                        },
                    },
                    {
                        "id": "world-tools",
                        "name": str(Path(__file__).with_name("dsh") / "world.mjs"),
                    },
                ]
            }
        )
        patch.write_text(json.dumps(rows, ensure_ascii=False))
        patch.chmod(0o600)
        self.harness = DeepSeekHarness(
            dsh_home=str(self.root / "home"),
            cwd=str(workspace),
            profile="sdk-minimal",
            patches=(str(patch),),
            provider="astcho-world",
            model=self.model.model,
            max_tokens=self.model.max_tokens,
            env={
                "DSH_SYSTEM_PROMPT": self.system_prompt,
                "ASTCHO_DSH_SOCKET": socket,
                "ASTCHO_DSH_BRIDGE_TOKEN": self.token,
                "ASTCHO_DSH_MODEL_KEY": self.model.api_key,
                "ASTCHO_DSH_SESSION_ID": self.session_state["id"],
            },
            initialize_timeout_seconds=30,
            # One SDK run can include many model/tool steps, including a 120s QQ call.
            # Do not impose a shorter total-turn timeout than the tools it hosts.
            request_timeout_seconds=None,
            shutdown_timeout_seconds=5,
        )
        try:
            await asyncio.to_thread(self.harness.start)
            if not self.bridge_ready:
                raise RuntimeError(
                    "World DSH plugin did not initialize; refusing an unconfigured session"
                )
            if self.session_state["started"]:
                self.session = await asyncio.to_thread(
                    self.harness.resume_session, self.session_state["id"]
                )
            else:
                self.session = self.harness.start_session(self.session_state["id"])
                # Materialize the SDK Session without asking the model to act.
                # Native durable timers need a live target even before the first wake.
                await asyncio.to_thread(
                    self.session.run,
                    json.dumps({"type": "world.maintenance", "action": "initialize"}),
                    on_notification=self._notification,
                )
                self.session_state["started"] = True
                self.kernel.store.set("dsh_session", self.session_state)
        except BaseException:
            await self.close()
            raise

    async def _bridge(self, reader, writer):
        task = asyncio.current_task()
        self.bridge_tasks.add(task)
        token = self.kernel.store.activity.set(self.active["id"] if self.active else None)
        try:
            try:
                async with asyncio.timeout(145):
                    # Context telemetry can exceed the App protocol's line buffer.
                    # Stream the complete JSON line; do not impose an input-token
                    # or logging-size cap that could abort an otherwise valid turn.
                    parts = []
                    while True:
                        part = await reader.read(65536)
                        if not part:
                            raise ConnectionError("World bridge closed before request completed")
                        parts.append(part)
                        if b"\n" in part:
                            break
                    request = json.loads(b"".join(parts))
                    if not hmac.compare_digest(request.get("token", ""), self.token):
                        raise ValueError("Invalid World bridge token")
                    result = await self.dispatch(request)
                    reply = {"result": result}
            except Exception as exc:
                reply = {"error": f"{type(exc).__name__}: {exc}"}
            writer.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            self.bridge_tasks.discard(task)
            self.kernel.store.activity.reset(token)
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    async def dispatch(self, request):
        method = request["method"]
        if method == "ready":
            self.bridge_ready = True
            return {}
        if method == "describe":
            instruction = Path(__file__).with_name("prompts") / "compaction.md"
            replaced = {"world_remind", "world_skills", "world_read_skill"}
            return {
                "tools": [t for t in self.tools.tools() if t["function"]["name"] not in replaced],
                "compaction_instruction": instruction.read_text(),
            }
        if method == "capabilities":
            self.kernel.store.set("dsh_capabilities", capability_status())
            return {}
        if method == "reminder_imports":
            self.kernel.store.set("reminder_backend", "dsh")
            return {
                "reminders": [
                    dict(r)
                    for r in self.kernel.store.db.execute("SELECT * FROM reminders WHERE fired=0")
                ]
            }
        if method == "reminder_imported":
            with self.kernel.store.db:
                self.kernel.store.db.execute(
                    "UPDATE reminders SET fired=2 WHERE id=? AND fired=0", (request["legacy_id"],)
                )
                mapping = self.kernel.store.get("dsh_reminder_imports", {})
                mapping[request["legacy_id"]] = request["native_id"]
                self.kernel.store.set("dsh_reminder_imports", mapping)
            return {}
        if method == "reminder_catalog":
            self.kernel.store.set("dsh_reminders", request["reminders"])
            return {}
        if method == "reminder_delivery":
            for item in request["reminders"]:
                self.kernel.store.notify(
                    "world",
                    {
                        "id": f"dsh:{item['schedule_id']}:{item['occurrence_at']}",
                        "source": "自己设置的提醒",
                        "message": item["reminder_prompt"],
                        "locator": {},
                    },
                )
            return {}
        if method == "model_request":
            from astcho_world.context_log import record_input
            from astcho_world.usage import provider_id

            record_input(
                self.kernel.store,
                {**request["data"], "billing_provider": provider_id(self.model.base_url)},
            )
            return {}
        if method == "model_usage":
            self.kernel.store.event(
                "debug.model_usage", "提供商报告的模型用量", data=request["data"]
            )
            return {}
        if method == "model_end":
            self.kernel.store.event("debug.model_end", "模型调用结束", data=request["data"])
            return {}
        if method == "continuity":
            from astcho_world.experience import operation_states

            return {
                "operations": operation_states(self.kernel.store, request["operation_ids"]),
                "logical_activity_id": (self.kernel.activities.current() or {}).get("id"),
            }
        if method == "activity_archive":
            return self.kernel.activities.archive(request["id"])
        if method == "activity_index":
            return self.kernel.activities.index(request["id"])
        if method == "activity_summary":
            return self.kernel.activities.summary(request["id"], request["summary"])
        if method == "activity_projection_result":
            self.kernel.store.set("activity_archive_error", request.get("error"))
            return {}
        if method == "project_video_result":
            from astcho_world.bilibili_details import retain_details, video_details

            row = self.kernel.store.db.execute(
                "SELECT seq,data FROM events WHERE kind='decision.result' "
                "AND json_extract(data,'$.tool')='bilibili__video' "
                "AND json_extract(data,'$.result.operation_id')=? "
                "AND json_extract(data,'$.call_id')=? ORDER BY seq DESC LIMIT 1",
                (request["operation_id"], request["call_id"]),
            ).fetchone()
            if row is None:
                raise ValueError("Original observed video result not found; history retained")
            result = json.loads(row["data"])["result"]
            info = result["data"]["info"]
            source_ref = retain_details(self.kernel.root / "apps" / "bilibili", info)
            data = {**video_details(info, source_ref), "original_result_event_seq": row["seq"]}
            projections = self.kernel.store.get("tool_result_projections", {})
            projections[request["operation_id"]] = data
            self.kernel.store.set("tool_result_projections", projections)
            return {"data": data, "source_event_seq": row["seq"]}
        if method == "context_inspection":
            self.kernel.store.set("context_inspection", request["data"])
            return {}
        if self.active is None:
            raise ValueError("No World activity is active")
        active = self.active
        if active.get("stopping"):
            if method == "step":
                return {"stop": True}
            return {"status": "cancelled", "reason": "activity interrupted"}
        if method == "step":
            if active["steps"] >= active["max_steps"]:
                active["budget_exhausted"] = True
                return {"stop": True}
            value = runtime_context(
                self.kernel,
                step=active["steps"],
                max_steps=active["max_steps"],
                previous_activity=active["previous"],
                wake_policy=self.wake_policy,
                provider=self.context_provider,
            )
            context = json.loads(value["content"])
            context["activity"]["run_id"] = active["id"]
            context["capabilities"] = self.kernel.store.get("dsh_capabilities", {})
            context["logical_activity"] = self.kernel.activities.current()
            if self.activity_context:
                context["recent_activities"] = self.kernel.activities.recent(self.recent_activities)
            # Deliver new arrivals only between completed tools, without cancelling writes.
            pending = self.kernel.store.pending_notification_ids()
            if pending:
                context["new_notifications"] = self.kernel.store.notifications()
            context["activity"]["history"] = "DSH durable session; older context may be compacted"
            active["steps"] += 1
            self.kernel.store.set("runtime_status", {"phase": "thinking", "updated": time.time()})
            self.kernel.store.event("model.started", "DSH 等待模型选择下一步")
            return {"stop": False, "context": context}
        if method in {"native_start", "native_result"}:
            return self._native_tool(request)
        if method == "execute":
            if active["finished_tool"]:
                return {"status": "cancelled", "reason": "activity finished"}
            name, args = request["name"], request["arguments"]
            definitions = {t["function"]["name"]: t["function"] for t in self.tools.tools()}
            if name not in definitions:
                raise ValueError("Unknown or undiscovered tool")
            validate(args, definitions[name]["parameters"])
            self.kernel.store.set(
                "runtime_status", {"phase": "executing", "tool": name, "updated": time.time()}
            )
            self.kernel.store.event(
                "decision.tool",
                f"选择 {name}",
                data={"tool": name, "arguments": args, "call_id": request["call_id"]},
            )
            result = await self.tools.execute(
                name, args, f"dsh:{self.session_state['id']}:{request['call_id']}"
            )
            active["finished_tool"] = name in {"world_finish", "world_pause"}
            self.kernel.store.event(
                "decision.result",
                "工具选择已返回结果",
                data={"tool": name, "call_id": request["call_id"], "result": result},
            )
            return result
        raise ValueError("Unknown World bridge method")

    def _native_tool(self, request):
        name, call_id = request["name"], request["call_id"]
        allowed = {
            "web_search",
            "web_fetch",
            "skill",
            "schedule_create",
            "schedule_list",
            "schedule_update",
            "schedule_delete",
        }
        if name not in allowed:
            raise ValueError("Unknown native capability")
        if self.active["finished_tool"] and request["method"] == "native_start":
            raise ValueError("World activity has finished")
        store = self.kernel.store
        operation = f"dsh:{self.session_state['id']}:{call_id}"
        if request["method"] == "native_start":
            effect = (
                "write"
                if name in {"schedule_create", "schedule_update", "schedule_delete"}
                else "read"
            )
            with store.db:
                store.db.execute(
                    "INSERT INTO operations VALUES(?,?,?,?,?,'running',NULL)",
                    (operation, "dsh", name, json.dumps(request["arguments"]), effect),
                )
            store.event(
                "decision.tool",
                f"选择 {name}",
                operation_id=operation,
                data={"tool": name, "arguments": request["arguments"], "call_id": call_id},
            )
            store.set(
                "runtime_status", {"phase": "executing", "tool": name, "updated": time.time()}
            )
        else:
            result = request["result"]
            row = store.db.execute(
                "SELECT effect FROM operations WHERE id=?", (operation,)
            ).fetchone()
            if row is None:
                raise ValueError("Native result has no admitted operation")
            status = "succeeded"
            if result.get("isError"):
                status = "unknown" if row[0] == "write" else "failed"
            with store.db:
                store.db.execute(
                    "UPDATE operations SET status=?,result=? WHERE id=?",
                    (
                        status,
                        json.dumps(result),
                        operation,
                    ),
                )
            store.observe("tool", operation, {"tool": name, "result": result})
            store.event(
                "decision.result",
                "DSH 工具返回结果",
                operation_id=operation,
                data={"tool": name, "call_id": call_id, "result": result},
            )
        return {}

    def _notification(self, notification):
        # SDK callbacks run in its synchronous runner thread. SQLite stays on World's loop.
        asyncio.run_coroutine_threadsafe(self._record(notification), self.loop).result(timeout=10)

    async def _record(self, notification):
        if notification.method != "session.event":
            return
        payload = notification.payload
        if payload.get("sessionId") != self.session_state["id"]:
            return
        event = payload.get("event", {})
        self.kernel.store.event("debug.dsh_event", "DSH 会话事件", data=event)
        if event.get("type") == "compaction/end":
            result = event.get("data", {})
            self.kernel.store.event(
                "compaction.finished",
                "经历压缩失败，保留未被替换的历史" if result.get("error") else "经历压缩已保存",
                data={**result, "status": "failed" if result.get("error") else "succeeded"},
            )
        if event.get("type") == "assistant/message":
            self.kernel.store.event(
                "model.finished", "DSH 模型返回选择", data=event.get("data", {})
            )

    async def compact(self):
        """Explicit maintenance; no new token budget, notification delivery or model decision."""
        return await self.run(_maintenance=True)

    async def inspect_context(self):
        """Local SDK surface/pressure inspection, without any model or App call."""
        async with self.lock:
            await self.start()
            result = await asyncio.to_thread(
                self.session.run,
                json.dumps({"type": "world.maintenance", "action": "inspect"}),
                on_notification=self._notification,
            )
            if result.finish_reason not in {"completed", "blocked"}:
                raise RuntimeError("Context inspection failed; history retained")
            return self.kernel.store.get("context_inspection")

    async def migrate_legacy(self):
        """Offline read-only maintenance, using the existing durable Session."""
        async with self.lock:
            await self.start()
            imported = self.kernel.activities.import_legacy()
            result = await asyncio.to_thread(
                self.session.run,
                json.dumps(
                    {
                        "type": "world.project_activities",
                        "finished_ids": [],
                        "legacy_ids": self.kernel.activities.legacy_ids(),
                        "keep": self.recent_activities,
                    }
                ),
                on_notification=self._notification,
            )
            error = self.kernel.store.get("activity_archive_error")
            if error:
                raise RuntimeError(error)
            return {"imported": len(imported), "finish_reason": result.finish_reason}

    async def refresh_activity_summaries(self):
        """Local index maintenance; compatibility name, no summary model or App action."""
        async with self.lock:
            await self.start()
            ids = self.kernel.activities.finished_ids()
            result = await asyncio.to_thread(
                self.session.run,
                json.dumps(
                    {"type": "world.project_activities", "finished_ids": ids, "refresh_ids": ids}
                ),
                on_notification=self._notification,
            )
            error = self.kernel.store.get("activity_archive_error")
            if error:
                raise RuntimeError(error)
            return {"activities": len(ids), "finish_reason": result.finish_reason}

    async def run(self, *, max_steps=20, wake_reasons=None, _maintenance=False, admin_request=None):
        async with self.lock:
            if max_steps < 1:
                raise ValueError("max_steps must be positive")
            previous = self.kernel.store.get("environment_activity")
            activity = {
                "id": uuid.uuid4().hex,
                "started": time.time(),
                "status": "running",
                "runner": "dsh",
            }
            token = self.kernel.store.activity.set(activity["id"])
            logical = None if _maintenance else self.kernel.activities.begin(activity["id"])
            self.active = {
                "id": activity["id"],
                "steps": 0,
                "max_steps": max_steps,
                "previous": previous,
                "finished_tool": False,
                "budget_exhausted": False,
            }
            self.kernel.store.set("environment_activity", activity)
            self.kernel.store.event("activity.started", "星回通过 DSH 开始一轮活动")
            try:
                await self.start()
                if _maintenance:
                    wake = {"type": "world.maintenance", "action": "compact"}
                else:
                    wake = self._wake(wake_reasons)
                    if admin_request is not None:
                        self.kernel.store.observe(
                            "admin_request", admin_request["id"], admin_request
                        )
                        wake["admin_request"] = admin_request

                self.session_state["started"] = True
                self.kernel.store.set("dsh_session", self.session_state)
                result = await asyncio.to_thread(
                    self.session.run,
                    json.dumps(wake, ensure_ascii=False),
                    on_notification=self._notification,
                )
                if result.finish_reason not in {"completed", "blocked", "max-tokens"}:
                    raise RuntimeError("DSH turn did not complete; inspect its session events")
                status = "idle"
                if self.active["budget_exhausted"] or result.finish_reason == "max-tokens":
                    status = "budget_exhausted"
                elif result.finish_reason == "blocked":
                    status = "blocked"
                final = {
                    "status": status,
                    "steps": self.active["steps"],
                    "runner": "dsh",
                    "session_id": self.session_state["id"],
                    "finish_reason": result.finish_reason,
                }
                self.kernel.store.set(
                    "environment_activity", {**activity, **final, "finished": time.time()}
                )
                self.kernel.store.set(
                    "runtime_status", {"phase": status, "updated": time.time(), "runner": "dsh"}
                )
                self.kernel.store.event("activity.finished", "DSH 本轮活动结束", data=final)
                if logical:
                    current = self.kernel.activities.current()
                    if current and current["state"] == "running":
                        self.kernel.activities.settle(
                            "suspended" if status != "idle" else "finished", finish_reason=status
                        )
                    if self.activity_context:
                        try:
                            projected = await asyncio.to_thread(
                                self.session.run,
                                json.dumps(
                                    {
                                        "type": "world.project_activities",
                                        "finished_ids": self.kernel.activities.finished_ids(),
                                        "keep": self.recent_activities,
                                    }
                                ),
                                on_notification=self._notification,
                            )
                            if projected.finish_reason not in {"completed", "blocked"}:
                                raise RuntimeError("Activity projection failed; history retained")
                            # A rejected maintenance turn is normal, but any error
                            # is reported by the plugin before concluding it.
                        except Exception as exc:
                            self.kernel.store.set("activity_archive_error", str(exc))
                return final
            except BaseException as exc:
                if logical:
                    self.kernel.activities.settle("suspended", suspend_reason="interrupted")
                self.active["stopping"] = True
                self.kernel.store.set(
                    "environment_activity",
                    {**activity, "status": "interrupted", "finished": time.time()},
                )
                self.kernel.store.event(
                    "activity.failed", "DSH 本轮活动中断", data={"error": str(exc)}
                )
                self.kernel.store.set("runtime_status", {"phase": "error", "updated": time.time()})
                await self.close()
                raise
            finally:
                self.active = None
                self.kernel.store.activity.reset(token)

    async def close(self):
        self.bridge_ready = False
        harness, self.harness = self.harness, None
        self.session = None
        if harness is not None:
            await asyncio.to_thread(harness.close)
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self.bridge_tasks):
            task.cancel()
        await asyncio.gather(*list(self.bridge_tasks), return_exceptions=True)
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def _wake(self, wake_reasons):
        self.kernel.store.tick()
        notifications = self.kernel.store.notifications()
        return {
            "type": "world.wake",
            "logical_activity_id": (self.kernel.activities.current() or {}).get("id"),
            "source": "world",
            "time": time.time(),
            "reasons": wake_reasons if wake_reasons is not None else [{"kind": "manual"}],
            "notifications": notifications,
            "notification_delivery": {
                "new_ids": [n["id"] for n in notifications],
                "list_limit": 100,
            },
        }
