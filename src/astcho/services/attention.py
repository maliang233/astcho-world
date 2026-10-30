from __future__ import annotations

import random
import time
from collections import deque

from astcho.domain.models import ChatMessage
from astcho.services.schedule import ScheduleState


class AttentionService:
    """Per-group context and deterministic identity-aware trigger state."""

    def __init__(
        self, bot_id: str, *, bot_name: str = "Astcho", base_talk_probability: float = 0.1
    ):
        self.bot_id = str(bot_id)
        self.bot_name = bot_name
        self.base_talk_probability = base_talk_probability
        self.messages: deque[ChatMessage] = deque(maxlen=30)
        self.observed_since: float | None = None
        self.last_bot_reply = 0.0
        self.consecutive_no_reply = 0
        self.watching_until = 0.0
        self._recent_meme_flags: deque[bool] = deque(maxlen=10)
        self.last_trace: dict[str, float | str | bool] = {}

    def add(self, message: ChatMessage) -> None:
        self.messages.append(message)
        if self.observed_since is None:
            self.observed_since = message.timestamp
        if message.is_bot or message.user_id == self.bot_id:
            self.last_bot_reply = message.timestamp

    def should_plan(
        self,
        message: ChatMessage,
        schedule: ScheduleState,
        *,
        excitement: float = 0.0,
        random_value: float | None = None,
    ) -> bool:
        direct = self.is_direct(message)
        if schedule.talk_value < 10:
            result = message.mentioned_bot or message.replied_to_bot
            self.last_trace = {
                "reason": "日程低活跃，仅响应提及/引用",
                "direct": result,
                "talk": schedule.talk_value,
                "result": result,
            }
            return result
        if direct:
            self.last_trace = {
                "reason": "直接提及、引用或呼名",
                "direct": True,
                "talk": schedule.talk_value,
                "result": True,
            }
            return True
        now = time.time()
        if self.observed_since is None:
            self.observed_since = min(message.timestamp, now)
        if self.last_bot_reply and now - self.last_bot_reply < 5:
            self.last_trace = {
                "reason": "回复冷却中",
                "direct": False,
                "talk": schedule.talk_value,
                "result": False,
                "silence": now - self.last_bot_reply,
            }
            return False
        silence = self._silence_seconds(now)
        silence_bonus = min(0.2, silence / 600 * 0.05)
        time_since_reply = now - self.last_bot_reply if self.last_bot_reply else float("inf")
        frequency_factor = (
            0.6
            if time_since_reply < 60
            else 0.75
            if time_since_reply < 180
            else 0.9
            if time_since_reply < 300
            else 1.0
        )
        threshold_factor = 1.2 if self.consecutive_no_reply >= 2 else 1.0
        watching_factor = 1.35 if now < self.watching_until else 1.0
        probability = (self.base_talk_probability + silence_bonus) * (schedule.talk_value / 100)
        probability *= frequency_factor * threshold_factor * watching_factor
        probability *= 1 + max(-1, min(1, excitement)) * 0.8
        sample = random.random() if random_value is None else random_value
        probability = max(0.0, min(1.0, probability))
        result = sample < probability
        self.last_trace = {
            "reason": "概率判定",
            "direct": False,
            "talk": schedule.talk_value,
            "silence": silence,
            "excitement": excitement,
            "frequency": frequency_factor,
            "probability": probability,
            "sample": sample,
            "result": result,
        }
        return result

    def is_direct(self, message: ChatMessage) -> bool:
        return (
            message.mentioned_bot
            or message.replied_to_bot
            or self.bot_name.lower() in message.text.lower()
        )

    def update_after_planner(self, replied: bool) -> None:
        if replied:
            self.consecutive_no_reply = 0
            self.watching_until = 0.0
        else:
            self.consecutive_no_reply += 1
            if self.consecutive_no_reply >= 2:
                self.watching_until = time.time() + 10

    def find_message(self, message_id: str | None) -> ChatMessage | None:
        if not message_id:
            return None
        wanted = str(message_id)
        return next(
            (message for message in reversed(self.messages) if message.message_id == wanted),
            None,
        )

    def context(self, limit: int = 20, *, now: float | None = None) -> str:
        """Render the legacy Maibot-style transcript consumed by both LLM stages."""
        current_time = time.time() if now is None else now
        lines = []
        for message in list(self.messages)[-limit:]:
            is_bot = message.user_id == self.bot_id or message.is_bot
            nickname = f"{self.bot_name}(你)" if is_bot else message.nickname
            suffixes = []
            if message.mentioned_bot:
                suffixes.append("【@了你】")
            if message.replied_to_bot:
                suffixes.append("【回复了你】")
            suffix = f" {' '.join(suffixes)}" if suffixes else ""
            image = f" [图片: {message.image_description}]" if message.image_description else ""
            reply_prefix = ""
            if message.reply_to_user_id:
                reply_name = (
                    "你"
                    if message.reply_to_user_id == self.bot_id
                    else message.reply_to_nickname or message.reply_to_user_id
                )
                quoted = f': "{message.reply_text}"' if message.reply_text else ""
                reply_prefix = f"[回复: {reply_name}{quoted}] "
            elapsed = max(0.0, current_time - message.timestamp)
            if elapsed < 60:
                relative_time = f"{int(elapsed)}秒前"
            elif elapsed < 3600:
                relative_time = f"{int(elapsed // 60)}分钟前"
            else:
                relative_time = f"{int(elapsed // 3600)}小时前"
            content = f"{reply_prefix}{message.text_with_mentions()}".rstrip()
            lines.append(
                f"[{message.message_id}] {relative_time}, {nickname}: {content}{suffix}{image}"
            )
        return "\n".join(lines)

    def seconds_since_bot_reply(self) -> int | None:
        if not self.last_bot_reply:
            return None
        return max(0, int(time.time() - self.last_bot_reply))

    def _silence_seconds(self, now: float) -> float:
        reference = self.last_bot_reply or self.observed_since
        return max(0.0, now - reference) if reference is not None else 0.0
