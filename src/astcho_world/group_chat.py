"""One subject composes group replies; legacy services still own chat mechanics."""

from __future__ import annotations

import asyncio
import random
import time
from copy import copy
from datetime import datetime
from types import SimpleNamespace

from astcho.domain.models import ChatMessage
from astcho.handlers.common import local_image_source, reply_message, split_reply
from astcho.handlers.group import _memory_contents_for_prompt
from astcho.services.attention import AttentionService
from astcho.services.emotion import Mood, apply_typo
from astcho_world.group_prompt import decision_guidance, reply_guidance

ATTENTION_FIELDS = ("observed_since", "last_bot_reply", "consecutive_no_reply", "watching_until")


class GroupChat:
    def __init__(self, runtime, bot):
        self.runtime, self.bot = runtime, bot

    def target(self, snapshot, target_id=None):
        if snapshot["kind"] != "group":
            raise ValueError("Group reply tools require a group observation")
        receiver = snapshot["chat_id"].removeprefix("group:")
        if snapshot["chat_id"] != f"group:{receiver}" or not receiver.isdecimal():
            raise ValueError("Live QQ requires group:<numeric ID>")
        allowed = self.runtime.settings.allowed_groups
        if allowed and receiver not in allowed:
            raise ValueError("Group is not in the configured allowlist")
        messages = [ChatMessage.model_validate(item) for item in snapshot["messages"]]
        eligible = [m for m in messages if not m.is_bot and m.user_id != str(self.bot.self_id)]
        wanted = target_id or snapshot.get("anchor_message_id")
        target = next((m for m in eligible if m.message_id == wanted), None)
        if target_id and target is None:
            raise ValueError("Reply target must be an observed incoming group message")
        if not eligible:
            raise ValueError("No observed incoming group message")
        return receiver, messages, target or eligible[-1]

    def attention(self, receiver, messages, state):
        # Restore the legacy Mood object in the new App's own persistent domain.
        # Reading can decay time-based state, but never applies a social decision.
        saved_mood = state.get("mood", {})
        self.runtime.emotion._moods.setdefault(
            receiver,
            Mood(
                excitement=saved_mood.get("excitement", state.get("excitement", 0)),
                shyness=saved_mood.get("shyness", 0),
                updated_at=saved_mood.get("updated_at", time.time()),
            ),
        )
        attention = AttentionService(
            str(self.bot.self_id), bot_name=str(self.runtime.settings.profile.get("name", "Astcho"))
        )
        for key in ATTENTION_FIELDS:
            if key in state:
                setattr(attention, key, state[key])
        previous = self.runtime.attention.get(receiver)
        if previous is not None:
            attention._recent_meme_flags = copy(previous._recent_meme_flags)
        for message in messages:
            attention.add(message)
        # A pinned historical observation cannot erase a newer confirmed send.
        attention.last_bot_reply = max(attention.last_bot_reply, state.get("last_bot_reply", 0))
        return attention

    def context(self, snapshot, state, target_id=None):
        receiver, messages, target = self.target(snapshot, target_id)
        runtime = self.runtime
        for message in messages:
            if not message.is_bot and message.user_id != str(self.bot.self_id):
                runtime.sqlite.touch_user(receiver, message.user_id, message.nickname)
        attention = self.attention(receiver, messages, state)
        mood = runtime.emotion.state(receiver, target.user_id)
        schedule = runtime.schedule.current()
        pending_ids = set(snapshot.get("pending_message_ids", []))
        pending = [m for m in messages if m.message_id in pending_ids] or [target]
        buffered = list(attention.messages)
        metadata = {
            "current_time": datetime.now().strftime("%H:%M:%S"),
            "accumulated_count": len(pending),
            "time_span_seconds": (
                int(buffered[-1].timestamp - buffered[0].timestamp) if len(buffered) > 1 else 0
            ),
            "participant_count": len({m.user_id for m in pending}),
            "last_bot_spoke_seconds": attention.seconds_since_bot_reply(),
        }
        memories = runtime.memory.retrieve(
            target.text or target.image_description,
            group_id=receiver,
            user_id=target.user_id,
            limit=runtime.settings.max_memories,
        )
        contents = _memory_contents_for_prompt(runtime, receiver, memories)
        context = attention.context(limit=15)
        expression = runtime.expressions.relevant_hint(receiver, context)
        return {
            "status": "available",
            "target_message_id": target.message_id,
            "target_user_id": target.user_id,
            "emotion": mood,
            "schedule": {
                "mood": schedule.mood,
                "routine": schedule.routine,
                "talk_value": schedule.talk_value,
            },
            "metadata": metadata,
            "decision_context": attention.context(limit=20),
            "memories": contents,
            "expression_hint": expression,
            "decision_guidance": decision_guidance(
                bot_name=str(runtime.settings.profile.get("name", "Astcho")), metadata=metadata
            ),
            "reply_guidance": reply_guidance(
                reasoning=runtime.settings.reasoning_enabled,
                bot_name=str(runtime.settings.profile.get("name", "Astcho")),
                profile=runtime.settings.profile,
                context=context,
                memories=contents,
                schedule=schedule.mood,
                emotion=mood,
                planner_reason="",
                expression_hint=expression,
                user_id=target.user_id,
                user_name=target.nickname,
                user_input=target.text or target.image_description,
            ),
        }

    async def execute(self, snapshot, state, decision, text, on_state, on_sent):
        receiver, messages, target = self.target(snapshot, decision.target_message_id)
        runtime = self.runtime
        async with runtime.locks[f"group:{receiver}"]:
            runtime.sqlite.touch_user(receiver, target.user_id, target.nickname)
            attention = self.attention(receiver, messages, state)
            runtime.attention[receiver] = attention
            runtime.emotion.state(receiver, target.user_id)
            mood = runtime.emotion.apply(
                receiver,
                target.user_id,
                excitement_delta=decision.excitement_delta,
                shyness_delta=decision.shyness_delta,
                affinity_score=decision.affinity_score,
            )
            attention.update_after_planner(decision.action == "reply")

            def save_state():
                group_mood = runtime.emotion._moods[receiver]
                on_state(
                    {
                        **{key: getattr(attention, key) for key in ATTENTION_FIELDS},
                        "excitement": mood["excitement"],
                        "mood": {
                            "excitement": group_mood.excitement,
                            "shyness": group_mood.shyness,
                            "updated_at": group_mood.updated_at,
                        },
                    }
                )

            save_state()
            if decision.action == "no_reply":
                return {}
            answer = apply_typo(text, mood["excitement"])
            parts = split_reply(answer)
            quote_id = target.message_id if target.mentioned_bot or target.replied_to_bot else None
            event = SimpleNamespace(group_id=receiver)
            # Keep legacy split/quote/pacing. Agent text is literal text, never CQ instructions.
            from nonebot.adapters.onebot.v11 import Message, MessageSegment

            async def delivered(response, part, image_description=""):
                sent = ChatMessage(
                    message_id=str(response["message_id"]),
                    user_id=str(self.bot.self_id),
                    nickname=str(runtime.settings.profile.get("name", "Astcho")),
                    text=part,
                    timestamp=time.time(),
                    is_bot=True,
                    image_description=image_description,
                )
                attention.add(sent)
                on_sent(sent)
                save_state()

            for index, part in enumerate(parts):
                message = Message()
                if index == 0 and quote_id:
                    message += MessageSegment.reply(int(quote_id))
                message += MessageSegment.text(part)
                response = await self.bot.send(event, message)
                await delivered(response, part)
                if index < len(parts) - 1:
                    await asyncio.sleep(random.uniform(0.8, 2.0))
            extra = {}
            if decision.should_meme and decision.meme_query:
                selected = await runtime.memes.select(
                    answer, decision.meme_query, session_key=receiver
                )
                if selected:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    local_path = selected.get("local_path")
                    try:
                        source = local_image_source(local_path) if local_path else selected["url"]
                        response = await self.bot.send(event, reply_message("", source))
                        await delivered(response, "", decision.meme_query)
                        runtime.memes.mark_used(selected["file_id"], session_key=receiver)
                    except Exception:
                        runtime.memes.delete(selected["file_id"])
                        extra["meme_status"] = "unknown"
            if target.text:
                runtime.memory.queue_turn(
                    target.text,
                    answer,
                    group_id=receiver,
                    user_id=target.user_id,
                    user_name=target.nickname,
                )
            return extra
