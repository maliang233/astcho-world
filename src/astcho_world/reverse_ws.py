"""OneBot reverse-WS transport and local IPC for the isolated QQ application."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from nonebot.adapters import Adapter as BaseAdapter
from nonebot.adapters.onebot.v11 import Adapter, Bot, MessageEvent
from nonebot.adapters.onebot.v11.bot import _check_at_me, _check_nickname, _check_reply
from nonebot.config import Config
from starlette.websockets import WebSocketDisconnect

from astcho_world.onebot import normalize_event


class BridgeAdapter(BaseAdapter):
    @classmethod
    def get_name(cls):
        return "OneBot V11"

    async def _call_api(self, bot, api, **data):
        return await self.driver.bridge.call(bot.self_id, api, data)


class WorldBot(Bot):
    """Keep original adapter preprocessing, then route to World instead of auto handlers."""

    async def handle_event(self, event):
        if isinstance(event, MessageEvent):
            raw_payload = event.model_dump(mode="json")
            event.message.reduce()
            await _check_reply(self, event)
            _check_at_me(self, event)
            _check_nickname(self, event)
            await self.adapter.driver.bridge.accept_event(self, event, raw_payload=raw_payload)


class ReverseWS:
    def __init__(self, kernel, ingress, *, bot_id=None, access_token=None, schedule_path=None):
        self.kernel, self.ingress = kernel, ingress
        self.expected_id = str(bot_id) if bot_id else kernel.store.get("qq_account")
        self.access_token = access_token
        self.schedule_path = str(schedule_path) if schedule_path else None
        self.socket = None
        self.bot_id = None
        self.pending = {}
        self.send_lock = asyncio.Lock()
        self.event_tasks = set()
        self.server = None
        self.directory = None
        self.socket_path = None

    async def start(self):
        # Short path avoids the macOS Unix-domain-socket path length limit.
        self.directory = tempfile.TemporaryDirectory(prefix="astcho-ws-", dir="/tmp")
        self.socket_path = str(Path(self.directory.name) / "transport.sock")
        self.server = await asyncio.start_unix_server(
            self._ipc, path=self.socket_path, limit=32 * 1024 * 1024
        )
        os.chmod(self.socket_path, 0o600)
        self._status("disconnected")

    def _status(self, state, *, heartbeat=None):
        value = {
            "transport": "reverse_ws",
            "state": state,
            "bot_id": self.bot_id,
            "updated": time.time(),
            "path": "/onebot/v11/ws",
        }
        if heartbeat is not None:
            value["last_heartbeat"] = heartbeat
        self.kernel.store.set("qq_connection", value)

    async def handle(self, websocket):
        account = websocket.headers.get("x-self-id", "")
        role = websocket.headers.get("x-client-role", "Universal")
        authorization = websocket.headers.get("authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization
            else (websocket.query_params.get("access_token", ""))
        )
        if self.access_token and not hmac.compare_digest(token, self.access_token):
            await websocket.close(1008, "Invalid access token")
            return
        if not account.isdecimal() or (self.expected_id and account != self.expected_id):
            await websocket.close(1008, "Invalid or different X-Self-ID")
            return
        if role != "Universal":
            await websocket.close(1008, "Configure a Universal reverse WebSocket client")
            return
        if self.socket is not None:
            await websocket.close(1008, "Account already connected")
            return
        await websocket.accept()
        self.socket, self.bot_id = websocket, account
        self.expected_id = account
        self.kernel.store.set("qq_account", account)
        config = Config(nickname=set())
        adapter = BridgeAdapter(SimpleNamespace(config=config, bridge=self))
        bot = WorldBot(adapter, account)
        try:
            if "qq" in self.kernel.apps:
                plugin = self.kernel.apps["qq"]
                # Retain transport configuration when this app's worker restarts.
                plugin.config["bot_id"] = account
                conversation = plugin.config.setdefault("conversation", {"allow_send": False})
                conversation["transport"] = {"kind": "reverse_ws", "socket_path": self.socket_path}
                if self.schedule_path:
                    conversation["schedule_path"] = self.schedule_path
                await plugin.request(
                    "connect",
                    {
                        "bot_id": account,
                        "transport": {"kind": "reverse_ws", "socket_path": self.socket_path},
                        "schedule_path": self.schedule_path,
                    },
                    10,
                )
            self._status("connected")
            self.kernel.store.event("connection.connected", "QQ 反向 WS 已连接", app="qq")
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                if "echo" in data:
                    future = self.pending.get(str(data["echo"]))
                    if future and not future.done():
                        future.set_result(data)
                    continue
                if data.get("self_id") is not None and str(data["self_id"]) != account:
                    await websocket.close(1008, "Event account mismatch")
                    break
                if data.get("meta_event_type") == "heartbeat":
                    self._status("connected", heartbeat=time.time())
                if event := Adapter.json_to_event(data):
                    task = asyncio.create_task(bot.handle_event(event))
                    self.event_tasks.add(task)
                    task.add_done_callback(self._event_done)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            self.kernel.store.event(
                "connection.failed",
                "QQ 连接处理失败",
                app="qq",
                data={"error_type": type(exc).__name__},
            )
            with contextlib.suppress(Exception):
                await websocket.close(1011, "QQ transport error")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("QQ reverse WebSocket disconnected"))
            for task in list(self.event_tasks):
                task.cancel()
            await asyncio.gather(*list(self.event_tasks), return_exceptions=True)
            self.socket = None
            self._status("disconnected")
            self.kernel.store.event("connection.disconnected", "QQ 反向 WS 已断开", app="qq")

    def _event_done(self, task):
        self.event_tasks.discard(task)
        if not task.cancelled() and task.exception():
            self.kernel.store.event(
                "connection.event_failed",
                "QQ 事件处理失败",
                app="qq",
                data={"error_type": type(task.exception()).__name__},
            )

    async def accept_event(self, bot, event, *, raw_payload):
        normalized = normalize_event(raw_payload, bot.self_id, parsed_event=event)
        if normalized:
            chat_id, message, metadata = normalized
            self.ingress.receive(chat_id, message, **metadata)

    async def call(self, bot_id, action, params):
        if self.socket is None or str(bot_id) != self.bot_id:
            raise ConnectionError("QQ is not connected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            async with self.send_lock:
                await self.socket.send_json({"action": action, "params": params, "echo": echo})
            async with asyncio.timeout(20):
                response = await future
            if response.get("retcode") != 0 or response.get("status") != "ok":
                raise RuntimeError("OneBot action was not confirmed")
            return response.get("data") or {}
        finally:
            self.pending.pop(echo, None)

    async def _ipc(self, reader, writer):
        try:
            async with asyncio.timeout(25):
                request = json.loads(await reader.readline())
                if request["action"] not in {
                    "get_msg",
                    "get_forward_msg",
                    "send_group_msg",
                    "send_private_msg",
                }:
                    raise ValueError("Action is not exposed by the QQ transport")
                result = await self.call(request["bot_id"], request["action"], request["params"])
                response = {"data": result}
        except Exception as exc:
            response = {"error": type(exc).__name__}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self):
        if self.socket is not None:
            with contextlib.suppress(Exception):
                await self.socket.close(1001)
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.directory is not None:
            self.directory.cleanup()


class WSDelivery:
    def __init__(self, bot_id, socket_path):
        self.self_id, self.socket_path = str(bot_id), socket_path

    async def call_api(self, action, **params):
        async with asyncio.timeout(25):
            reader, writer = await asyncio.open_unix_connection(
                self.socket_path, limit=32 * 1024 * 1024
            )
            try:
                writer.write(
                    json.dumps(
                        {"bot_id": self.self_id, "action": action, "params": params}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                response = json.loads(await reader.readline())
                if response.get("error"):
                    raise ConnectionError("QQ WS action failed: " + response["error"])
                return response["data"]
            finally:
                writer.close()
                await writer.wait_closed()

    async def send(self, event, message):
        params = {"message": [{"type": s.type, "data": s.data} for s in message]}
        if hasattr(event, "group_id"):
            params["group_id"] = int(event.group_id)
            action = "send_group_msg"
        else:
            params["user_id"] = int(event.user_id)
            action = "send_private_msg"
        result = await self.call_api(action, **params)
        if "message_id" not in result:
            raise RuntimeError("OneBot returned no delivery receipt")
        return result
