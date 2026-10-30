import asyncio
import json

from fastapi.testclient import TestClient

from astcho_world.apps.qq import MANIFEST
from astcho_world.host import create_app
from astcho_world.onebot import normalize_event


def payload():
    return {
        "time": 1,
        "self_id": 10001,
        "post_type": "message",
        "sub_type": "normal",
        "user_id": 10002,
        "message_type": "group",
        "message_id": 123,
        "message": [
            {"type": "at", "data": {"qq": "10001"}},
            {"type": "text", "data": {"text": "说句话"}},
        ],
        "raw_message": "[CQ:at,qq=10001]说句话",
        "font": 0,
        "sender": {"user_id": 10002, "nickname": "QQ昵称", "card": "群名片"},
        "group_id": 10003,
        "to_me": True,
    }


def test_inbound_uses_original_nickname_and_direction_parsers():
    chat, message, metadata = normalize_event(payload(), "10001")
    assert chat == "group:10003"
    assert message["nickname"] == "QQ昵称"
    assert message["mentioned_bot"] is True
    assert message["at_targets"] == ["你"]
    assert metadata == {"kind": "group"}


def test_read_only_host_accepts_authenticated_event_without_model_or_send(tmp_path, monkeypatch):
    manifest_path = tmp_path / "qq.json"
    manifest_path.write_text(MANIFEST.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": [{"manifest": "qq.json"}]}))
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    app = create_app(
        config_path=config,
        data_dir=tmp_path / "data",
        bot_id="10001",
        ingress_token="synthetic-test-token",
        model=None,
        qq_transport="http",
    )
    with TestClient(app) as client:
        denied = client.post("/onebot/events", json=payload())
        assert denied.status_code == 401
        wrong = {**payload(), "self_id": 99999}
        headers = {"Authorization": "Bearer synthetic-test-token"}
        assert client.post("/onebot/events", json=wrong, headers=headers).status_code == 400
        accepted = client.post("/onebot/events", json=payload(), headers=headers)
        assert accepted.status_code == 200
        assert accepted.json() == {"accepted": True}
        # Shutdown drains the original notification window without invoking a model.
    import sqlite3

    db = sqlite3.connect(tmp_path / "data" / "world.sqlite3")
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    db.close()


def test_media_input_is_not_silently_treated_as_text_only(tmp_path):
    from astcho_world.apps.qq import QQ

    async def scenario():
        app = QQ(tmp_path, {"conversation": {"allow_send": True}})
        data = payload()
        data["message"].append({"type": "image", "data": {"url": "https://example.test/x.png"}})
        chat, message, metadata = normalize_event(data, "10001")
        app.ingest({"batch_id": "media", "chat_id": chat, "messages": [message], **metadata})
        opened = app.read({"chat_id": chat})
        assert opened["requires_media"] is True
        result = await app.dispatch(
            "call",
            {
                "tool": "participate",
                "operation_id": "p1",
                "arguments": {"observation_id": opened["observation_id"]},
            },
        )
        assert result["status"] == "unavailable"
        assert app.conversation is None

    asyncio.run(scenario())
