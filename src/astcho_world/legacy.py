from __future__ import annotations

from copy import copy

from astcho.services.attention import AttentionService


class OpenedAttention(AttentionService):
    """An explicit participation action has already passed the notification/attention gate."""

    def should_plan(self, *args, **kwargs):
        self.last_trace = {"reason": "显式选择参与已打开会话", "result": True}
        return True


async def participate_in_group(runtime, bot, *, group_id, observed_messages, pending):
    """Call the unchanged legacy execution chain using only an observed snapshot.

    ``runtime`` must own new-environment data. ``bot`` is an explicit delivery transport,
    not the environment agent's text output. This function never creates a live bot,
    copies production configuration or registers the original automatic handlers.

    The caller must serialize all participation in this subject and must not replay a
    failed/unknown invocation. Original sends and state updates are intentionally intact.
    """
    from astcho.handlers.group import _process_after_window

    known = {message.message_id for message in observed_messages}
    if not pending or any(message.message_id not in known for _, message in pending):
        raise ValueError("Pending messages must belong to the observed snapshot")
    if runtime.pending_group_messages.get(group_id):
        raise ValueError("Another participation is pending")
    previous = runtime.attention.get(group_id)
    opened = OpenedAttention(
        str(bot.self_id), bot_name=str(runtime.settings.profile.get("name", "Astcho"))
    )
    if previous is not None:
        for key in ("observed_since", "last_bot_reply", "consecutive_no_reply", "watching_until"):
            setattr(opened, key, getattr(previous, key))
        opened._recent_meme_flags = copy(previous._recent_meme_flags)
    for message in observed_messages:
        opened.add(message)
    runtime.attention[group_id] = opened
    runtime.pending_group_messages[group_id] = list(pending)
    # No reimplementation of prompts, target selection, mood, reply, typo, split,
    # quoting, pacing, memes, degraded-reply handling or memory queueing.
    await _process_after_window(runtime, bot, group_id)


async def private_reply(runtime, *, user_id, nickname, observed_history, latest):
    """Protected private generation entry; delivery remains the caller's explicit action.

    Does not imitate or register the original automatic/private-admin handler. The
    history must already be observed; persisting the assistant entry requires delivery.
    """
    history = list(observed_history)[-50:]
    return await runtime.chat.private_reply(nickname=nickname, history=history, latest=latest)
