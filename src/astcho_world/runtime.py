from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from pathlib import Path
from typing import Protocol

from jsonschema import validate

from astcho_world.kernel import Kernel
from astcho_world.prompting import environment_prompt, runtime_context
from astcho_world.toolbox import WorldTools

ENVIRONMENT_PROMPT = environment_prompt()


class Model(Protocol):
    async def decide(self, messages: list[dict], tools: list[dict]) -> dict: ...


class Agent(WorldTools):
    def __init__(
        self,
        kernel: Kernel,
        model: Model,
        *,
        skills: Path | None = None,
        profile_path: Path | None = None,
        context_provider=None,
        wake_policy=None,
    ):
        self.kernel, self.model, self.skills = kernel, model, skills
        kernel.store.set("reminder_backend", "native")
        self.system_prompt = environment_prompt(profile_path)
        self.context_provider = context_provider
        self.wake_policy = wake_policy or {"mode": "single_run"}
        self.lock = asyncio.Lock()

    async def run(self, *, max_steps=20, wake_reasons=None, admin_request=None):
        async with self.lock:
            if max_steps < 1:
                raise ValueError("max_steps must be positive")
            activity_id = uuid.uuid4().hex
            previous = self.kernel.store.get("environment_activity")
            activity = {"id": activity_id, "started": time.time(), "status": "running"}
            self.kernel.store.set("environment_activity", activity)
            token = self.kernel.store.activity.set(activity_id)
            self.kernel.store.event("activity.started", "星回开始一轮活动")
            try:
                result = await self._run(
                    max_steps=max_steps,
                    wake_reasons=wake_reasons,
                    previous_activity=previous,
                    admin_request=admin_request,
                )
                self.kernel.store.set(
                    "environment_activity",
                    {
                        **activity,
                        **result,
                        "finished": time.time(),
                    },
                )
                self.kernel.store.event("activity.finished", "本轮活动结束", data=result)
                self.kernel.store.set(
                    "runtime_status", {"phase": result["status"], "updated": time.time()}
                )
                return result
            except BaseException as exc:
                self.kernel.store.set(
                    "environment_activity",
                    {
                        **activity,
                        "status": "interrupted",
                        "finished": time.time(),
                    },
                )
                self.kernel.store.event(
                    "activity.failed",
                    "本轮活动中断",
                    data={"error": str(exc), "traceback": traceback.format_exc()},
                )
                self.kernel.store.set("runtime_status", {"phase": "error", "updated": time.time()})
                raise
            finally:
                self.kernel.store.activity.reset(token)

    async def _run(self, *, max_steps, wake_reasons, previous_activity, admin_request=None):
        history = self.kernel.store.get("agent_history", [])
        reasons = list(wake_reasons) if wake_reasons is not None else [{"kind": "manual"}]
        # Resume a durable assistant tool batch before asking the model for another one.
        if history:
            index = next(
                (i for i in range(len(history) - 1, -1, -1) if history[i]["role"] == "assistant"),
                None,
            )
            if index is not None and history[index].get("tool_calls"):
                completed = {
                    item["tool_call_id"] for item in history[index + 1 :] if item["role"] == "tool"
                }
                pending = any(call["id"] not in completed for call in history[index]["tool_calls"])
                if pending:
                    reasons.append({"kind": "recovery"})
                    if await self._complete_tools(history, index):
                        return {"status": "idle", "steps": 0}
        self.kernel.store.tick()
        if admin_request is not None:
            self.kernel.store.observe("admin_request", admin_request["id"], admin_request)
            history.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "type": "world.admin_request",
                            "source": "world",
                            "admin_request": admin_request,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        notifications = self.kernel.store.notifications()
        # Wake supplies time, not unread application contents or a mandatory task.
        history.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "time": time.time(),
                        "type": "world.wake",
                        "source": "world",
                        "reasons": reasons,
                        "notifications": notifications,
                        "notification_delivery": {
                            "new_ids": [n["id"] for n in notifications],
                            "list_limit": 100,
                        },
                    },
                    ensure_ascii=False,
                ),
            }
        )
        self.kernel.store.set("agent_history", history)
        for _ in range(max_steps):
            self.kernel.store.set("runtime_status", {"phase": "thinking", "updated": time.time()})
            started = time.monotonic()
            self.kernel.store.event("model.started", "等待模型选择下一步")
            messages = [
                {"role": "system", "content": self.system_prompt},
                *self.context(history),
                runtime_context(
                    self.kernel,
                    step=_,
                    max_steps=max_steps,
                    previous_activity=previous_activity,
                    wake_policy=self.wake_policy,
                    provider=self.context_provider,
                ),
            ]
            tools = self.tools()
            request_id = uuid.uuid4().hex
            self.kernel.store.event(
                "debug.model_request",
                "模型请求详情",
                data={
                    "messages": messages,
                    "tools": tools,
                    "request_id": request_id,
                    "model": getattr(self.model, "model", "unknown"),
                    "billing_provider": getattr(self.model, "billing_provider", "unknown"),
                },
            )
            try:
                response = await self.model.decide(messages, tools)
            except BaseException:
                self.kernel.store.event(
                    "debug.model_end",
                    "模型调用失败",
                    data={
                        "request_id": request_id,
                        "status": "failed",
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
                raise
            usage = getattr(self.model, "last_usage", None)
            if usage is not None:
                self.kernel.store.event(
                    "debug.model_usage",
                    "模型实际用量",
                    data={"request_id": request_id, "usage": usage, "format": "openai"},
                )
            self.kernel.store.event(
                "debug.model_end",
                "模型调用结束",
                data={
                    "request_id": request_id,
                    "status": "succeeded",
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
            self.kernel.store.event(
                "model.finished",
                "模型返回选择",
                data={
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "content": response.get("content"),
                    "tool_calls": response.get("tool_calls", []),
                },
            )
            history.append(response)
            # Persist call IDs before any side effect, so restart cannot regenerate them.
            self.kernel.store.set("agent_history", history)
            calls = response.get("tool_calls", [])
            finished = not calls or await self._complete_tools(history, len(history) - 1)
            if finished:
                return {"status": "idle", "steps": _ + 1}
        return {"status": "budget_exhausted", "steps": max_steps}

    @staticmethod
    def context(history):
        # Bound model input at complete wake/turn boundaries. Durable records remain on disk.
        start = max(0, len(history) - 100)
        while start > 0 and history[start]["role"] != "user":
            start -= 1
        return history[start:]

    async def _complete_tools(self, history, index):
        calls = history[index].get("tool_calls", [])
        completed = {
            item["tool_call_id"] for item in history[index + 1 :] if item["role"] == "tool"
        }
        finished = False
        for call in calls:
            if call["id"] in completed:
                finished |= call["function"]["name"] == "world_finish"
                continue
            if finished:
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(
                            {"status": "cancelled", "reason": "activity finished"}
                        ),
                    }
                )
                self.kernel.store.set("agent_history", history)
                continue
            try:
                name = call["function"]["name"]
                args = json.loads(call["function"]["arguments"])
                definitions = {tool["function"]["name"]: tool["function"] for tool in self.tools()}
                if name not in definitions:
                    raise ValueError("Unknown or undiscovered tool")
                validate(args, definitions[name]["parameters"])
                self.kernel.store.set(
                    "runtime_status", {"phase": "executing", "tool": name, "updated": time.time()}
                )
                self.kernel.store.event(
                    "decision.tool",
                    f"选择 {name}",
                    data={"tool": name, "arguments": args, "call_id": call["id"]},
                )
                result = await self.execute(name, args, f"agent:{index}:{call['id']}")
                finished |= name == "world_finish"
            except Exception as exc:
                self.kernel.store.event(
                    "debug.tool_error",
                    "工具调用异常",
                    data={"call_id": call["id"], "traceback": traceback.format_exc()},
                )
                result = {"error": f"{type(exc).__name__}: {exc}"}
            self.kernel.store.event(
                "decision.result",
                "工具选择已返回结果",
                data={"tool": call["function"]["name"], "call_id": call["id"], "result": result},
            )
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            self.kernel.store.set("agent_history", history)
        return finished


class OpenAIModel:
    """Optional environment-only client. Never used to wrap legacy ChatService requests."""

    def __init__(self, *, model, api_key, base_url):
        from openai import AsyncOpenAI

        from astcho_world.usage import provider_id

        self.model = model
        self.billing_provider = provider_id(base_url)
        self.last_usage = None
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def decide(self, messages, tools):
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
        )
        self.last_usage = response.usage.model_dump() if response.usage else None
        return response.choices[0].message.model_dump(
            include={"role", "content", "tool_calls"},
            exclude_none=True,
        )
