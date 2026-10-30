import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from nonebot.adapters.onebot.v11 import Message
from starlette.websockets import WebSocketDisconnect

from astcho_world.apps.qq import MANIFEST
from astcho_world.host import create_app
from astcho_world.reverse_ws import WSDelivery

TOKEN = "synthetic-ws-access-token"
HEADERS = {"X-Self-ID": "10001", "X-Client-Role": "Universal", "Authorization": f"Bearer {TOKEN}"}


def make_app(tmp_path):
    (tmp_path / "qq.json").write_text(MANIFEST.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": [{"manifest": "qq.json"}]}))
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps(
            {
                "default": [
                    {"start": "00:00", "end": "23:59", "talk_value": 75, "mood": "原日程测试"},
                ]
            }
        )
    )
    return create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id=None,
        ingress_token=None,
        ws_token=TOKEN,
        schedule_path=schedule,
    )


def event(mid=123, reply=False):
    segments = [
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": "说句话"}},
    ]
    if reply:
        segments.insert(0, {"type": "reply", "data": {"id": "777"}})
    return {
        "time": 1,
        "self_id": 10001,
        "post_type": "message",
        "sub_type": "normal",
        "user_id": 10002,
        "message_type": "group",
        "message_id": mid,
        "message": segments,
        "raw_message": "@星回 说句话",
        "font": 0,
        "sender": {"user_id": 10002, "nickname": "真实昵称", "card": "群名片"},
        "group_id": 10003,
    }


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def connected(app):
    return app.state.kernel.store.get("qq_connection", {}).get("state") == "connected"


def test_reverse_ws_learns_identity_and_routes_events_without_auto_reply(tmp_path, monkeypatch):
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/onebot/events", json={}).status_code == 404
        with client.websocket_connect("/onebot/v11/ws", headers=HEADERS) as ws:
            client.portal.call(wait_for, lambda: connected(app))
            assert client.portal.call(app.state.kernel.store.get, "qq_account") == "10001"
            ws.send_json(event())
            client.portal.call(
                wait_for,
                lambda: (
                    app.state.kernel.store.db.execute(
                        "SELECT count(*) FROM notifications"
                    ).fetchone()[0]
                    == 1
                ),
            )

            def counts():
                store = app.state.kernel.store
                return [
                    store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("observations", "operations")
                ]

            assert client.portal.call(counts) == [0, 0]
            assert app.state.ingress.schedule_provider()["mood"] == "原日程测试"
        client.portal.call(wait_for, lambda: not connected(app))


def test_reverse_ws_delivery_uses_same_connection_and_correlated_receipt(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers=HEADERS) as ws:
            client.portal.call(wait_for, lambda: connected(app))

            async def deliver():
                bot = WSDelivery("10001", app.state.qq_bridge.socket_path)
                return await bot.send(SimpleNamespace(group_id="10003"), Message("合成投递测试"))

            pending = client.portal.start_task_soon(deliver)
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            assert action["params"]["group_id"] == 10003
            ws.send_json(
                {"status": "ok", "retcode": 0, "data": {"message_id": 901}, "echo": action["echo"]}
            )
            assert pending.result(timeout=5) == {"message_id": 901}


def test_reply_preprocessing_reuses_original_adapter_get_msg(tmp_path, monkeypatch):
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers=HEADERS) as ws:
            client.portal.call(wait_for, lambda: connected(app))
            ws.send_json(event(reply=True))
            lookup = ws.receive_json()
            assert lookup["action"] == "get_msg"
            assert lookup["params"] == {"message_id": 777}
            ws.send_json(
                {
                    "status": "ok",
                    "retcode": 0,
                    "echo": lookup["echo"],
                    "data": {
                        "time": 1,
                        "message_type": "group",
                        "message_id": 777,
                        "real_id": 777,
                        "sender": {"user_id": 10001, "nickname": "星回"},
                        "message": [{"type": "text", "data": {"text": "我之前说的话"}}],
                    },
                }
            )
            client.portal.call(
                wait_for,
                lambda: (
                    app.state.kernel.store.db.execute(
                        "SELECT count(*) FROM notifications"
                    ).fetchone()[0]
                    == 1
                ),
            )

            async def read():
                return await app.state.kernel.call(
                    "qq", "read_messages", {"chat_id": "group:10003"}
                )

            result = client.portal.call(read)
            message = result.data["messages"][0]
            assert message["replied_to_bot"] is True
            assert message["reply_to_message_id"] == "777"
            assert message["reply_to_user_id"] == "10001"
            # The installed adapter's Reply model has no get_plaintext method;
            # retain the original reply_details_of behavior instead of enhancing it.
            assert message["reply_text"] == ""
            assert message["nickname"] == "真实昵称"


def test_disconnect_does_not_replay_pending_send_and_reconnects_same_account(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers=HEADERS) as ws:
            client.portal.call(wait_for, lambda: connected(app))

            async def deliver():
                return await WSDelivery("10001", app.state.qq_bridge.socket_path).send(
                    SimpleNamespace(group_id="10003"), Message("结果未知的投递")
                )

            pending = client.portal.start_task_soon(deliver)
            assert ws.receive_json()["action"] == "send_group_msg"
        client.portal.call(wait_for, lambda: not connected(app))
        with pytest.raises(ConnectionError):
            pending.result(timeout=5)
        with client.websocket_connect("/onebot/v11/ws", headers=HEADERS):
            client.portal.call(wait_for, lambda: connected(app))
            assert not app.state.qq_bridge.pending


@pytest.mark.parametrize(
    "headers",
    [
        {**HEADERS, "Authorization": "Bearer wrong"},
        {**HEADERS, "X-Self-ID": "bad"},
        {**HEADERS, "X-Client-Role": "API"},
    ],
)
def test_reverse_ws_rejects_invalid_handshake(tmp_path, headers):
    with TestClient(make_app(tmp_path)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/onebot/v11/ws", headers=headers):
                pass
        assert exc.value.code == 1008
