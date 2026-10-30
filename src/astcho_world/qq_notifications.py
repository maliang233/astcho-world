"""World's QQ notification eligibility; the protected probability formula is reused."""

from astcho.services.attention import AttentionService


def raw_segments(raw_event):
    segments = (raw_event or {}).get("message", [])
    if isinstance(segments, str):
        # OneBot also permits CQ-encoded strings. Parse their structure without
        # treating quoted text, reply/at parameters or media URLs as message prose.
        from nonebot.adapters.onebot.v11 import Message

        return [{"type": s.type, "data": s.data} for s in Message(segments)]
    return segments if isinstance(segments, list) else []


def has_nontext_content(raw_event):
    return any(s.get("type") not in {"text", "at", "reply"} for s in raw_segments(raw_event))


def has_content(message, raw_event=None):
    if message.text.strip() or message.image_description.strip():
        return True
    return any(
        s.get("type") not in {"text", "at", "reply"}
        or (s.get("type") == "text" and str(s.get("data", {}).get("text", "")).strip())
        for s in raw_segments(raw_event)
    )


def empty_addressing(message, raw_event=None):
    addressed = (
        message.mentioned_bot
        or message.replied_to_bot
        or message.reply_to_message_id
        or message.at_targets
        or any(s.get("type") in {"reply", "at"} for s in raw_segments(raw_event))
    )
    return bool(addressed and not has_content(message, raw_event))


def notification_candidate(message, *, bot_name, raw_event=None):
    if not has_content(message, raw_event):
        return False
    bare_name = (
        message.text.strip().casefold() == bot_name.strip().casefold()
        and not message.mentioned_bot
        and not message.replied_to_bot
        and not message.at_targets
        and not message.reply_to_message_id
        and not any(s.get("type") in {"at", "reply"} for s in raw_segments(raw_event))
        and not message.image_description.strip()
        and not has_nontext_content(raw_event)
    )
    return not bare_name


class NotificationAttention(AttentionService):
    """Only actual QQ addressing bypasses sampling; names in prose do not."""

    def is_direct(self, message):
        return message.mentioned_bot or message.replied_to_bot
