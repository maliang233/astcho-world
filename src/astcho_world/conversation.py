from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from astcho.domain.models import ChatMessage
from astcho.services.attention import AttentionService
from astcho_world.legacy import participate_in_group


class OneBotDelivery:
    """Explicit delivery only. Constructed lazily after operator enables this backend."""

    def __init__(self, bot_id, api_url, token):
        import httpx

        self.self_id = bot_id
        self.client = httpx.AsyncClient(
            base_url=api_url.rstrip("/") + "/",
            timeout=20,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )

    async def send(self, event, message):
        segments = [{"type": item.type, "data": item.data} for item in message]
        if hasattr(event, "group_id"):
            endpoint = "send_group_msg"
            payload = {"group_id": int(event.group_id), "message": segments}
        else:
            endpoint = "send_private_msg"
            payload = {"user_id": int(event.user_id), "message": segments}
        response = await self.client.post(endpoint, json=payload)
        response.raise_for_status()
        result = response.json()
        if result.get("retcode") != 0 or result.get("status") != "ok":
            raise RuntimeError("OneBot did not confirm message delivery")
        data = result.get("data") or {}
        if "message_id" not in data:
            raise RuntimeError("OneBot returned no delivery receipt")
        return data


class Conversation:
    """Lazy bridge to the protected legacy runtime, with its own data directory."""

    def __init__(self, data_dir, config):
        self.data_dir, self.config = data_dir, config
        self.runtime = None
        self.bot = None

    def initialize(self):
        if self.runtime is not None:
            return
        from dotenv import load_dotenv

        from astcho.config import Settings
        from astcho.runtime import Runtime

        if not self.config.get("allow_send", False):
            raise ValueError("Conversation delivery is disabled in this installation")
        if self.config.get("env_file"):
            load_dotenv(self.config["env_file"], override=False)
        bot_id = self.config.get("expected_bot_id") or os.environ["ASTCHO_WORLD_BOT_ID"]
        if bot_id != self.config.get("expected_bot_id", bot_id):
            raise ValueError("QQ inbox and delivery backend use different bot IDs")
        settings = Settings.from_env(load_file=False)
        if self.config.get("schedule_path"):
            settings = replace(settings, schedule_path=Path(self.config["schedule_path"]))
        # Never share the old instance's mutable database, even if its env file was selected.
        settings = replace(settings, data_dir=self.data_dir / "cognition")
        runtime = Runtime.build(settings)
        from astcho_world.usage_capture import instrument_legacy

        instrument_legacy(runtime, self.data_dir)
        transport = self.config.get("transport", {})
        if transport.get("kind") == "reverse_ws":
            from astcho_world.reverse_ws import WSDelivery

            self.bot = WSDelivery(bot_id, transport["socket_path"])
        else:
            self.bot = OneBotDelivery(
                bot_id, os.environ["ASTCHO_WORLD_QQ_API"], os.getenv("ASTCHO_WORLD_QQ_TOKEN", "")
            )
        self.runtime = runtime

    async def participate(self, snapshot, attention_state):
        self.initialize()
        runtime, bot = self.runtime, self.bot
        chat_id = snapshot["chat_id"]
        kind = snapshot["kind"]
        receiver = chat_id.removeprefix(f"{kind}:")
        if not receiver.isdecimal():
            raise ValueError("Live QQ requires group:<numeric ID> or private:<numeric ID>")
        if kind == "group" and runtime.settings.allowed_groups:
            if receiver not in runtime.settings.allowed_groups:
                raise ValueError("Group is not in the configured allowlist")
        messages = [ChatMessage.model_validate(item) for item in snapshot["messages"]]
        for message in messages:
            if not message.is_bot and message.user_id != str(bot.self_id):
                runtime.sqlite.touch_user(receiver, message.user_id, message.nickname)
        eligible = [m for m in messages if not m.is_bot and m.user_id != str(bot.self_id)]
        if not eligible:
            raise ValueError("No observed incoming message to participate in")
        anchor = snapshot.get("anchor_message_id")
        trigger = next((m for m in eligible if m.message_id == anchor), eligible[-1])
        if kind == "private":
            if self.config.get("private_direct"):
                # Same private handler contract as the protected source: own
                # per-user deque, append latest before request, no tool exposure.
                key = f"private:{receiver}"
                async with runtime.locks[key]:
                    if not trigger.text or trigger.text.startswith(("/", ".")):
                        return {"status": "completed", "sent": []}
                    handled, answer = runtime.expressions.handle_admin_feedback(
                        receiver, trigger.text
                    )
                    if not handled:
                        history = runtime.private_histories[key]
                        history.append({"role": "user", "content": trigger.text})
                        answer = await runtime.chat.private_reply(
                            nickname=snapshot.get("private_nickname", trigger.nickname or "朋友"),
                            history=list(history),
                            latest=trigger.text,
                        )
                        history.append({"role": "assistant", "content": answer})
                    return await self._send_private(receiver, answer)
            # The legacy private prompt uses at most 50 entries and cleans the prefix itself.
            history = [
                {
                    "role": "assistant" if m.is_bot or m.user_id == str(bot.self_id) else "user",
                    "content": m.text,
                }
                for m in messages
            ][-50:]
            answer = await runtime.chat.private_reply(
                nickname=trigger.nickname,
                history=history,
                latest=trigger.text,
            )
            return await self._send_private(receiver, answer)
        attention = AttentionService(
            str(bot.self_id), bot_name=runtime.settings.profile.get("name")
        )
        for key in ("observed_since", "last_bot_reply", "consecutive_no_reply", "watching_until"):
            if key in attention_state:
                setattr(attention, key, attention_state[key])
        runtime.attention[receiver] = attention
        event = SimpleNamespace(group_id=receiver)
        pending_ids = set(snapshot.get("pending_message_ids", []))
        pending = [(event, m) for m in messages if m.message_id in pending_ids]
        await participate_in_group(
            runtime,
            bot,
            group_id=receiver,
            observed_messages=messages,
            pending=pending or [(event, trigger)],
        )
        attention = runtime.attention[receiver]
        sent = [
            m.model_dump()
            for m in attention.messages
            if m.is_bot and m.message_id not in {item.message_id for item in messages}
        ]
        mood = runtime.emotion.state(receiver, trigger.user_id)
        return {
            "status": "completed",
            "sent": sent,
            "attention": {
                **{
                    key: getattr(attention, key)
                    for key in (
                        "observed_since",
                        "last_bot_reply",
                        "consecutive_no_reply",
                        "watching_until",
                    )
                },
                "excitement": mood["excitement"],
            },
        }

    async def _send_private(self, receiver, answer):
        from nonebot.adapters.onebot.v11 import Message

        sent = await self.bot.send(SimpleNamespace(user_id=receiver), Message(answer))
        return {
            "status": "completed",
            "sent": [
                {
                    "message_id": str(sent["message_id"]),
                    "text": answer,
                    "user_id": str(self.bot.self_id),
                    "nickname": self.runtime.settings.profile.get("name", "星回"),
                    "timestamp": time.time(),
                    "is_bot": True,
                }
            ],
        }
