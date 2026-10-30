"""Facts for social judgment, never a reply score, quota, or unread-content channel."""


def participation_context(app, snapshot, observed_at):
    window = 600
    since = observed_at - window
    # Own delivered/observed messages are already known actions. Do not aggregate
    # other people's unseen records, even when they are present in the App DB.
    own = app.db.execute(
        "SELECT SUM(CASE WHEN json_extract(body,'$.timestamp')>=? THEN 1 ELSE 0 END),"
        "MAX(json_extract(body,'$.timestamp')) FROM messages WHERE chat=? "
        "AND (json_extract(body,'$.is_bot')=1 OR json_extract(body,'$.user_id')=?) "
        "AND json_extract(body,'$.timestamp')<=?",
        (since, snapshot["chat_id"], app.bot_id, observed_at),
    ).fetchone()
    last_own = own[1]
    observed = [
        message for message in snapshot["messages"] if since <= message["timestamp"] <= observed_at
    ]
    others = [
        message
        for message in observed
        if not message["is_bot"] and message["user_id"] != app.bot_id
    ]
    return {
        "as_of": observed_at,
        "window_seconds": window,
        "own": {
            "scope": "own messages known to this installation in this chat",
            "message_count": own[0] or 0,
            "last_message_at": last_own,
            "unit": "QQ messages, including separate text segments, images and shares; not turns",
        },
        "observed_conversation": {
            "scope": "only messages in this snapshot within the window; may be partial",
            "message_count": len(observed),
            "other_message_count": len(others),
            "other_participant_count": len({message["user_id"] for message in others}),
            "other_messages_after_last_own": (
                sum(message["timestamp"] > last_own for message in others)
                if last_own is not None
                else None
            ),
        },
    }
