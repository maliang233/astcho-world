"""Synthetic sends only: no real QQ/Bilibili side effects."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from jsonschema import validate

from astcho_world.apps.qq import MANIFEST, QQ
from astcho_world.qq_sharing import share_context, share_link
from tests.test_group_subject import incoming, setup_group

URL = "https://www.bilibili.com/video/BV1xx411c7mD/"
OTHER = "https://www.bilibili.com/video/BV1d8pU6xEZX/"


def sharing_app(tmp_path, monkeypatch):
    app, *_ = setup_group(tmp_path)
    now = [time.time()]
    clock = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr("astcho_world.qq_sharing.time", clock)
    monkeypatch.setattr("astcho_world.apps.qq.time", clock)
    app.config["private_admins"] = ["10004"]
    sends = []

    async def send(event, message):
        sends.append(
            {
                "group": getattr(event, "group_id", None),
                "user": event.user_id,
                "message": str(message),
            }
        )
        return {"message_id": 900 + len(sends)}

    app.delivery_bot = lambda: SimpleNamespace(send=send)
    return app, now, sends


def group_args(app, **overrides):
    return {
        "chat_id": "group:10003",
        "url": URL,
        "intent_id": "one",
        "observation_id": app.read({"chat_id": "group:10003"})["observation_id"],
        "audience_reason": "回应这次实际观察到的群内话题",
        **overrides,
    }


def test_group_guard_is_recipient_scoped_persistent_and_does_not_queue(tmp_path, monkeypatch):
    app, now, sends = sharing_app(tmp_path, monkeypatch)

    async def scenario():
        args = group_args(app)
        first = await share_link(app, args, "one")
        assert first["status"] == "completed"
        assert sends[0]["message"] == URL  # Internal reason is never sent.
        assert (await share_link(app, group_args(app, url=OTHER, intent_id="two"), "two"))[
            "reason"
        ] == "group_share_cooldown"
        assert len(sends) == 1
        assert app.db.execute("SELECT count(*) FROM link_shares").fetchone()[0] == 1
        now[0] += 601
        # Replaying an old receipt succeeds even after its observation expired.
        assert await share_link(app, args, "one") == first
        assert len(sends) == 1
        again = await share_link(app, group_args(app, intent_id="later"), "later")
        assert again["status"] == "completed"  # Explicit later re-share remains possible.
        metadata = share_context(app, "group:10003")
        assert metadata["target"]["shared_last_24h"] == 2
        assert metadata["target"]["last_shared_at"] == now[0]
        assert metadata["sharing"]["preferred_private_targets"] == ["private:10004"]
        validate(app.ambient_state(), MANIFEST.ambient_schema)
        app.db.close()
        restarted = QQ(tmp_path, app.config)
        restarted.delivery_bot = app.delivery_bot
        denied = await share_link(restarted, group_args(restarted, url=OTHER), "after-restart")
        assert denied["reason"] == "group_share_cooldown"
        assert len(sends) == 2
        restarted.db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case,reason",
    [
        ("missing", "read_group_context_first"),
        ("expired", "group_context_expired"),
        ("changed", "group_context_changed"),
        ("wrong_group", "read_group_context_first"),
        ("old_anchor", "group_context_changed"),
        ("no_reason", "audience_reason_required"),
    ],
)
def test_group_share_needs_real_current_context_without_leaking_new_body(
    tmp_path, monkeypatch, case, reason
):
    app, now, sends = sharing_app(tmp_path, monkeypatch)
    args = group_args(app)
    if case == "missing":
        args.pop("observation_id")
    if case == "expired":
        now[0] += 301
    if case in {"changed", "old_anchor"}:
        app.ingest(
            {
                "batch_id": "new",
                "chat_id": "group:10003",
                "messages": [incoming("124", text="UNREAD_SECRET_NEW_TOPIC")],
            }
        )
    if case == "old_anchor":
        args["observation_id"] = app.read({"chat_id": "group:10003", "anchor_message_id": "123"})[
            "observation_id"
        ]
    if case == "wrong_group":
        app.ingest({"batch_id": "different", "chat_id": "group:10006", "messages": []})
        args["observation_id"] = app.read({"chat_id": "group:10006"})["observation_id"]
    if case == "no_reason":
        args["audience_reason"] = " "
    result = asyncio.run(share_link(app, args, case))
    assert result["reason"] == reason
    assert "UNREAD_SECRET_NEW_TOPIC" not in json.dumps(result)
    assert sends == []
    assert app.db.execute("SELECT count(*) FROM link_shares").fetchone()[0] == 0
    app.db.close()


def test_private_admin_does_not_need_incoming_message_group_context_or_cooldown(
    tmp_path, monkeypatch
):
    app, now, sends = sharing_app(tmp_path, monkeypatch)
    app.ingest(
        {
            "batch_id": "private",
            "chat_id": "private:10004",
            "kind": "private",
            "messages": [incoming("dm", user_id="10004", text="PRIVATE_UNREAD_SECRET")],
        }
    )

    async def scenario():
        for n in range(2):
            result = await share_link(
                app, {"chat_id": "private:10004", "url": URL, "intent_id": str(n)}, str(n)
            )
            assert result["status"] == "completed"
        assert all(s["group"] is None and s["user"] == 10004 for s in sends)
        assert share_context(app, "private:10004")["target"]["group_next_allowed_at"] is None
        assert "PRIVATE_UNREAD_SECRET" not in json.dumps(app.ambient_state())
        with pytest.raises(ValueError, match="target"):
            await share_link(app, {"chat_id": "private:10005", "url": URL}, "ordinary")

    asyncio.run(scenario())
    app.db.close()


def test_unknown_receipt_blocks_new_intent_and_timestamp_link_variant(tmp_path, monkeypatch):
    app, now, sends = sharing_app(tmp_path, monkeypatch)

    async def uncertain(*_args):
        sends.append("attempted")
        raise ConnectionError("Synthetic lost receipt")

    app.delivery_bot = lambda: SimpleNamespace(send=uncertain)

    async def scenario():
        args = group_args(app)
        with pytest.raises(ConnectionError):
            await share_link(app, args, "lost")
        now[0] += 1000
        retry = await share_link(app, group_args(app, url=URL + "?t=12", intent_id="new"), "new")
        assert retry["reason"] == "previous_share_outcome_unknown"
        assert share_context(app, "group:10003")["target"]["unknown_count"] == 1
        assert sends == ["attempted"]
        assert (await share_link(app, args, "lost"))["status"] == "unknown"

    asyncio.run(scenario())
    app.db.close()


def test_old_share_schema_backfills_only_confirmed_outgoing_timestamps(tmp_path):
    app, *_ = setup_group(tmp_path)
    app.db.execute("DROP TABLE link_shares")
    app.db.execute(
        "CREATE TABLE link_shares(operation TEXT PRIMARY KEY,chat TEXT,url TEXT,"
        "text TEXT,result TEXT)"
    )
    app.db.execute(
        "INSERT INTO messages(chat,mid,body) VALUES(?,?,?)",
        (
            "group:10003",
            "sent",
            json.dumps(incoming("sent", user_id=app.bot_id, is_bot=True, timestamp=1234)),
        ),
    )
    receipt = {"status": "completed", "sent_message_id": "sent", "chat_id": "group:10003"}
    app.db.execute(
        "INSERT INTO link_shares VALUES(?,?,?,?,?)",
        ("old", "group:10003", URL, "", json.dumps(receipt)),
    )
    app.db.execute(
        "INSERT INTO link_shares VALUES(?,?,?,?,NULL)", ("unknown", "group:10003", OTHER, "")
    )
    app.db.commit()
    app.db.close()
    for _ in range(2):
        restarted = QQ(tmp_path, app.config)
        rows = dict(restarted.db.execute("SELECT operation,created FROM link_shares").fetchall())
        assert rows == {"old": 1234, "unknown": None}
        assert restarted.db.execute(
            "SELECT result FROM link_shares WHERE operation='old'"
        ).fetchone()[0] == json.dumps(receipt)
        restarted.db.close()
