from __future__ import annotations

import base64
import re
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment


def text_of(event: MessageEvent) -> str:
    return event.get_plaintext().strip()


def at_targets_of(event: MessageEvent, bot_id: str) -> list[str]:
    """Render OneBot at segments the way the legacy group context did."""
    targets = []
    for segment in event.get_message():
        if segment.type != "at":
            continue
        qq = str(segment.data.get("qq", "")).strip()
        name = str(segment.data.get("name", "")).strip()
        if qq == str(bot_id):
            target = "你"
        elif qq == "all":
            target = "全体成员"
        elif name:
            target = name
        elif qq:
            target = f"QQ:{qq}"
        else:
            continue
        if target not in targets:
            targets.append(target)
    return targets


def reply_details_of(event: MessageEvent) -> tuple[str | None, str | None, str, str]:
    """Return the quoted message id, sender identity and text exposed by OneBot."""
    reply = getattr(event, "reply", None)
    if not reply:
        return None, None, "", ""
    sender = getattr(reply, "sender", None)
    user_id = str(getattr(sender, "user_id", "") or "") or None
    nickname = str(getattr(sender, "nickname", "") or "").strip()
    if not nickname:
        nickname = str(getattr(sender, "card", "") or "").strip()
    if not nickname and user_id:
        nickname = user_id
    get_plaintext = getattr(reply, "get_plaintext", None)
    text = str(get_plaintext() if callable(get_plaintext) else "").strip()[:100]
    message_id = str(getattr(reply, "message_id", "") or "") or None
    return message_id, user_id, nickname, text


def image_urls(event: MessageEvent) -> list[str]:
    return [
        str(segment.data.get("url", ""))
        for segment in event.get_message()
        if segment.type == "image" and segment.data.get("url")
    ]


def media_summary(event: MessageEvent) -> str:
    parts = []
    for segment in event.get_message():
        if segment.type in {"video", "record"}:
            source = segment.data.get("url") or segment.data.get("file") or "media"
            parts.append(f"[{segment.type}: {source}]")
        elif segment.type == "forward":
            parts.append(f"[合并转发消息: {segment.data.get('id', '')}]")
    return " ".join(parts)


def reply_message(
    text: str, image_url: str | None = None, *, reply_to: str | None = None
) -> Message:
    message = Message()
    if reply_to:
        message += MessageSegment.reply(int(reply_to))
    message += Message(text)
    if image_url:
        message += MessageSegment.image(image_url)
    return message


def split_reply(text: str, maximum_parts: int = 3) -> list[str]:
    """Keep the old short, conversational multi-bubble delivery style."""
    cleaned = re.sub(r"^(?:Astcho|星回|回复)\s*[:：]\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"</?(?:thinking|reply)>", "", cleaned, flags=re.I).strip()
    parts = [part.strip() for part in re.split(r"\n+", cleaned) if part.strip()]
    if len(parts) <= maximum_parts:
        return parts or ["……"]
    return parts[: maximum_parts - 1] + [" ".join(parts[maximum_parts - 1 :])]


def local_image_source(path: str) -> str:
    """Encode host-local media because NapCat runs in a separate container filesystem."""
    payload = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"base64://{payload}"
