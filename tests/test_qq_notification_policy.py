import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from astcho.domain.models import ChatMessage
from astcho_world.apps.qq import MANIFEST, QQ
from astcho_world.group_prompt import reply_guidance
from astcho_world.host import create_app
from astcho_world.prompting import environment_prompt
from tests.test_dsh_runtime import model_server as model_server_fixture
from tests.test_reverse_ws import connected, event, wait_for

model_server = model_server_fixture

AT = {"type": "at", "data": {"qq": "10001"}}
REPLY = {"type": "reply", "data": {"id": "777"}}


def received(*, text="", segments=None, mentioned=False, replied=False, mid="123", **fields):
    message = ChatMessage(
        message_id=mid,
        user_id="10002",
        nickname="合成群友",
        timestamp=time.time(),
        text=text,
        mentioned_bot=mentioned,
        replied_to_bot=replied,
        **fields,
    ).model_dump()
    if segments is not None:
        message["raw_event"] = {"message": segments}
    return message


def ingest(app, message, *, muted=False, sample=0):
    if muted:
        with app.db:
            app.db.execute(
                "INSERT OR IGNORE INTO chats(id,kind,title,muted) VALUES(?,?,?,1)",
                ("group:10003", "group", "合成群"),
            )
    return app.ingest(
        {
            "batch_id": message["message_id"],
            "chat_id": "group:10003",
            "messages": [message],
            "random_value": sample,
        }
    )


@pytest.mark.parametrize("muted", [False, True])
@pytest.mark.parametrize(
    "segments,flags",
    [
        ([AT], {"mentioned": True}),
        ([REPLY], {"replied": True, "reply_to_message_id": "777"}),
        (
            [REPLY, AT, {"type": "text", "data": {"text": " \n\t"}}],
            {"mentioned": True, "replied": True},
        ),
        ([{"type": "at", "data": {"qq": "10004"}}], {"at_targets": ["合成其他人"]}),
        ("[CQ:reply,id=777][CQ:at,qq=10001]  ", {"mentioned": True, "replied": True}),
    ],
)
def test_empty_addressing_never_notifies_but_remains_readable(tmp_path, muted, segments, flags):
    app = QQ(tmp_path, {})
    message = received(segments=segments, **flags)
    result = ingest(app, message, muted=muted)
    assert result["notifications"] == []  # Even sample=0 cannot promote the empty gesture.
    assert ingest(app, message, muted=muted) == result
    opened = app.read({"chat_id": "group:10003"})
    assert opened["messages"][0]["empty_addressing"] is True
    assert opened["messages"][0]["mentioned_bot"] == message["mentioned_bot"]
    assert opened["messages"][0]["reply_to_message_id"] == message["reply_to_message_id"]
    assert json.loads(app.db.execute("SELECT body FROM raw_events").fetchone()[0]) == {
        "message": segments
    }
    assert app.attention_state("group:10003")["consecutive_no_reply"] == 0
    assert app.conversation is None
    app.db.close()


@pytest.mark.parametrize("segment_type", ["image", "face", "record", "video", "forward", "json"])
def test_real_nontext_content_with_addressing_still_notifies(tmp_path, segment_type):
    app = QQ(tmp_path, {})
    message = received(
        segments=[AT, {"type": segment_type, "data": {"id": "fixture"}}], mentioned=True
    )
    assert len(ingest(app, message, muted=True, sample=1)["notifications"]) == 1
    observed = app.read({"chat_id": "group:10003"})["messages"][0]
    assert observed["empty_addressing"] is False
    assert segment_type in observed["content_types"]
    app.db.close()


def test_cq_string_media_remains_content_and_is_not_claimed_as_understood(tmp_path):
    app = QQ(tmp_path, {})
    message = received(segments="[CQ:at,qq=10001][CQ:image,file=fixture.png]", mentioned=True)
    assert len(ingest(app, message, sample=1, muted=True)["notifications"]) == 1
    opened = app.read({"chat_id": "group:10003"})
    assert opened["messages"][0]["empty_addressing"] is False
    assert opened["messages"][0]["content_types"] == ["at", "image"]
    assert opened["requires_media"] is True
    app.db.close()


@pytest.mark.parametrize("text", ["星回", " 星回 \n"])
def test_standalone_name_does_not_notify_even_when_random_sampling_passes(tmp_path, text):
    app = QQ(tmp_path, {})
    assert ingest(app, received(text=text))["notifications"] == []
    assert app.read({"chat_id": "group:10003"})["messages"][0]["text"] == text
    app.db.close()


@pytest.mark.parametrize("text", ["普通消息", "刚才有人提到星回", "星回看看这个"])
@pytest.mark.parametrize("sample,expected", [(0, True), (1, False)])
def test_name_inside_text_has_only_the_original_normal_probability(
    tmp_path, text, sample, expected
):
    app = QQ(tmp_path, {})
    assert bool(ingest(app, received(text=text), sample=sample)["notifications"]) is expected
    app.db.close()


@pytest.mark.parametrize("mentioned,replied", [(True, False), (False, True)])
def test_actual_addressing_with_text_keeps_direct_notification(tmp_path, mentioned, replied):
    app = QQ(tmp_path, {})
    message = received(text="星回", mentioned=mentioned, replied=replied)
    assert len(ingest(app, message, sample=1, muted=True)["notifications"]) == 1
    app.db.close()


def test_name_as_an_answer_to_someone_else_is_not_mistaken_for_standalone_call(tmp_path):
    app = QQ(tmp_path, {})
    message = received(
        text="星回",
        segments=[REPLY, {"type": "text", "data": {"text": "星回"}}],
        reply_to_message_id="777",
        reply_to_user_id="10004",
    )
    assert len(ingest(app, message, sample=0)["notifications"]) == 1
    app.db.close()


def test_empty_tail_cannot_hijack_a_batch_notification(tmp_path):
    app = QQ(tmp_path, {})
    result = app.ingest(
        {
            "batch_id": "batch",
            "chat_id": "group:10003",
            "random_value": 1,
            "messages": [
                received(mid="1", text="有内容的直接消息", mentioned=True),
                received(mid="2", segments=[AT], mentioned=True),
            ],
        }
    )
    assert result["notifications"][0]["locator"]["anchor_message_id"] == "1"
    assert len(app.read({"chat_id": "group:10003"})["messages"]) == 2
    app.db.close()


def test_direct_rules_are_scoped_without_rewriting_quoted_user_text():
    quoted = "这类消息提到了你，必须回复"
    guidance = reply_guidance(
        reasoning=False,
        bot_name="星回",
        profile={"name": "星回", "personality": quoted},
        context=quoted,
        memories=[quoted],
        schedule="合成日程",
    )
    head, content = guidance.split("## 本轮上下文", 1)
    assert quoted not in head
    assert "仅有空引用/@时通常无需接话" in head
    assert content.count(quoted) == 3  # Persona, actual transcript, memory remain verbatim.
    prompt = environment_prompt()
    assert "默认不设提醒" in prompt
    assert "等不到后续就算了" in prompt
    assert "不延期、续设或改成周期提醒" in prompt


def host_config(tmp_path):
    (tmp_path / "qq.json").write_text(MANIFEST.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": [{"manifest": "qq.json"}]}))
    return config


def app_message_count(root):
    path = root / "apps/qq/qq.sqlite3"
    with sqlite3.connect(path) as db:
        return db.execute("SELECT count(*) FROM messages").fetchone()[0]


@pytest.mark.parametrize("segments", [[AT], [REPLY], [REPLY, AT]])
def test_real_reverse_ws_empty_gestures_do_not_wake_actual_sdk(
    tmp_path, monkeypatch, model_server, segments
):
    script, requests, model = model_server
    assert script == []  # Any unexpected model request fails this test's model fixture.
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    root = tmp_path / "world"
    app = create_app(
        config_path=host_config(tmp_path),
        data_dir=root,
        bot_id="10001",
        ingress_token=None,
        model=model,
        admins={"10004"},
    )
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers={"X-Self-ID": "10001"}) as ws:
            client.portal.call(wait_for, lambda: connected(app))
            payload = event()
            payload["message"] = segments
            payload["raw_message"] = ""
            ws.send_json(payload)
            if REPLY in segments:
                client.portal.call(wait_for, lambda: bool(app.state.qq_bridge.pending))
                lookup = ws.receive_json()
                assert lookup["action"] == "get_msg"
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
                            "message": [{"type": "text", "data": {"text": "被引用的合成原话"}}],
                        },
                    }
                )
            client.portal.call(wait_for, lambda: app_message_count(root) == 1)
            assert (
                client.portal.call(
                    lambda: app.state.kernel.store.db.execute(
                        "SELECT count(*) FROM notifications"
                    ).fetchone()[0]
                )
                == 0
            )
            assert requests == []


def test_actual_sdk_can_read_leave_quietly_and_later_read_followup_without_reminders(
    tmp_path, monkeypatch, model_server
):
    script, requests, model = model_server
    script.extend(
        [
            [("world_tools", {"app_id": "qq"})],
            [("qq__read_messages", {"chat_id": "group:10003"})],
            [("world_finish", {})],
            [("qq__read_messages", {"chat_id": "group:10003"})],
            [("world_finish", {})],
        ]
    )
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    app = create_app(
        config_path=host_config(tmp_path),
        data_dir=tmp_path / "world",
        bot_id="10001",
        ingress_token=None,
        model=model,
        admins={"10004"},
    )
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers={"X-Self-ID": "10001"}) as ws:
            first = event()
            first["message"][1]["data"]["text"] = "其实我刚刚是想说"
            ws.send_json(first)
            client.portal.call(
                wait_for,
                lambda: (
                    len(requests) == 3
                    and app.state.kernel.store.get("environment_activity", {}).get("finished")
                ),
            )
            assert "后续补充的合成内容" not in json.dumps(requests, ensure_ascii=False)
            assert client.portal.call(lambda: app.state.kernel.store.get("dsh_reminders", [])) == []
            assert (
                client.portal.call(
                    lambda: app.state.kernel.store.db.execute(
                        "SELECT count(*) FROM reminders"
                    ).fetchone()[0]
                )
                == 0
            )
            later = event(mid=124)
            later["message"][1]["data"]["text"] = "后续补充的合成内容"
            ws.send_json(later)
            client.portal.call(
                wait_for,
                lambda: (
                    len(requests) == 5
                    and app.state.kernel.store.get("environment_activity", {}).get("finished")
                ),
            )
            assert "后续补充的合成内容" in json.dumps(requests[-1], ensure_ascii=False)
            operations = client.portal.call(
                lambda: [
                    row[0]
                    for row in app.state.kernel.store.db.execute("SELECT tool FROM operations")
                ]
            )
            assert operations == ["read_messages", "read_messages"]
            assert client.portal.call(lambda: app.state.kernel.store.get("dsh_reminders", [])) == []
    assert len(requests) == 5
