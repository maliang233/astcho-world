import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from astcho_world.apps.bilibili import MANIFEST as BILI
from astcho_world.apps.qq import MANIFEST as QQ
from astcho_world.host import create_app
from astcho_world.kernel import Kernel
from astcho_world.speech import outbound_speech_guidance
from tests.test_dsh_runtime import model_server as model_server_fixture
from tests.test_group_subject import setup_group
from tests.test_qq_images import PNG
from tests.test_qq_images import image_server as image_server_fixture
from tests.test_reverse_ws import connected, event, wait_for

model_server = model_server_fixture
image_server = image_server_fixture
BV = "BV1xx411c7mD"
URL = f"https://www.bilibili.com/video/{BV}/"


@pytest.fixture
def bili_server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            name = body["tool"]
            result = {
                "subtitles": {
                    "segments": [{"from": 0, "to": 4, "content": "Synthetic subtitle fact"}],
                    "page": 1,
                },
                "view": {
                    "metadata": [{"covered_sec": 4, "pages": 2}],
                    "images": [{"data": base64.b64encode(PNG).decode(), "mime": "image/png"}],
                },
                "like": {"liked": True},
                "comment": {"comment_id": "555"},
                "favorites": {"folder_id": 9, "state": True},
                "share": {"resource": {"type": "link", "url": URL, "id": BV}},
            }.get(name, {"status": "available"})
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_real_sdk_bili_read_frame_interact_and_share_over_reverse_ws(
    tmp_path, monkeypatch, model_server, bili_server, image_server
):
    script, requests, model = model_server
    backend, seen = bili_server
    vision, vision_seen = image_server
    for key, value in {
        "ASTCHO_LLM_API_KEY": "synthetic",
        "ASTCHO_LLM_BASE_URL": vision + "/v1",
        "ASTCHO_CHAT_MODEL": "synthetic-chat",
        "ASTCHO_PLANNER_MODEL": "synthetic-planner",
        "ASTCHO_VISION_MODEL": "synthetic-vision",
        "ASTCHO_VISION_API_KEY": "synthetic",
        "ASTCHO_VISION_BASE_URL": vision + "/v1",
        "ASTCHO_ADMINS": "10004",
        "ASTCHO_MEMORY_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    profile = tmp_path / "profile.json"
    profile.write_text('{"name":"星回","identity":{"self_cognition":"合成人格原文"}}')
    monkeypatch.setenv("ASTCHO_PROFILE_PATH", str(profile))
    monkeypatch.setenv("ASTCHO_ALLOWED_GROUPS", "10003")

    def share_to_observed_group():
        results = [
            json.loads(m["content"])
            for m in requests[-1]["messages"]
            if m["role"] == "tool" and m["content"].startswith("{")
        ]
        snapshot = next(
            r["data"] for r in reversed(results) if "observation_id" in r.get("data", {})
        )
        assert snapshot["share_context"]["target"]["shared_last_24h"] == 0
        return [
            (
                "qq__share_link",
                {
                    "chat_id": "group:10003",
                    "url": URL,
                    "intent_id": "share-1",
                    "observation_id": snapshot["observation_id"],
                    "audience_reason": "回应实际读到的群内提问",
                },
            )
        ]

    script.extend(
        [
            [("world_tools", {"app_id": "bilibili"})],
            [("bilibili__subtitles", {"bvid": BV})],
            [("bilibili__view", {"bvid": BV, "kind": "frames", "question": "合成画面是什么？"})],
            [("bilibili__like", {"bvid": BV, "liked": True, "intent_id": "like-1"})],
            [("bilibili__comment", {"bvid": BV, "text": "合成评论", "intent_id": "comment-1"})],
            [
                (
                    "bilibili__favorites",
                    {"action": "add", "bvid": BV, "folder_id": 9, "intent_id": "favorite-1"},
                )
            ],
            [("bilibili__share", {"bvid": BV})],
            [("world_tools", {"app_id": "qq"})],
            [("qq__share_context", {"chat_id": "group:10003"})],
            [("qq__read_messages", {"chat_id": "group:10003"})],
            share_to_observed_group,
            [("world_finish", {})],
        ]
    )
    (tmp_path / "qq.json").write_text(QQ.model_dump_json())
    (tmp_path / "bili.json").write_text(BILI.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(
        json.dumps(
            {
                "apps": [
                    {"manifest": "qq.json", "config": {"conversation": {"allow_send": True}}},
                    {"manifest": "bili.json", "config": {"test_backend": backend}},
                ]
            }
        )
    )
    app = create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id="10001",
        ingress_token=None,
        model=model,
        admins={"10004"},
    )
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers={"X-Self-ID": "10001"}) as ws:
            client.portal.call(wait_for, lambda: connected(app))
            ws.send_json(event())
            client.portal.call(wait_for, lambda: bool(app.state.qq_bridge.pending))
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            assert action["params"]["message"] == [{"type": "text", "data": {"text": URL}}]
            ws.send_json(
                {"status": "ok", "retcode": 0, "data": {"message_id": 901}, "echo": action["echo"]}
            )
            client.portal.call(
                wait_for,
                lambda: app.state.kernel.store.get("environment_activity", {}).get("finished"),
            )
            text = json.dumps(requests, ensure_ascii=False)
            assert "Synthetic subtitle fact" in text
            # Commenting and sharing see the original speaking rules throughout
            # the SDK chain, including before any group reply_context is read.
            assert all(
                outbound_speech_guidance() in request["messages"][0]["content"]
                for request in requests
            )
            assert "合成细看结果" in text
            assert '"comment_id"' in text or "comment_id" in text
            assert len(vision_seen["requests"]) == 1
            assert [s["tool"] for s in seen] == [
                "subtitles",
                "view",
                "like",
                "comment",
                "favorites",
                "share",
            ]

            async def stop_bili():
                await app.state.kernel.stop_app("bilibili")
                return await app.state.kernel.call("qq", "list_chats", {})

            assert client.portal.call(stop_bili).status == "succeeded"


def test_write_dedup_and_ordinary_private_share_rejected(tmp_path, bili_server):
    backend, seen = bili_server

    async def scenario():
        kernel = Kernel(tmp_path / "world")
        kernel.register(BILI, config={"test_backend": backend})
        try:
            args = {"bvid": BV, "liked": True, "intent_id": "like-1"}
            one = await kernel.call("bilibili", "like", args, operation_id="once")
            two = await kernel.call("bilibili", "like", args, operation_id="once")
            assert one == two
            await kernel.call(
                "bilibili", "like", {**args, "intent_id": "like-2"}, operation_id="new-intent"
            )
            assert len(seen) == 2
        finally:
            await kernel.close()

    asyncio.run(scenario())
    from astcho_world.qq_sharing import share_link

    app, *_ = setup_group(tmp_path)
    app.config["private_admins"] = ["10004"]
    with pytest.raises(ValueError, match="target|administrators"):
        asyncio.run(share_link(app, {"chat_id": "private:10005", "url": URL}, "denied"))


def test_unknown_intent_cannot_replay_under_new_model_call_id(tmp_path, bili_server):
    backend, seen = bili_server

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(BILI, config={"test_backend": backend})
        args = {"bvid": BV, "text": "合成评论", "intent_id": "one-comment"}
        try:
            kernel.store.db.execute(
                "INSERT INTO operations(id,app,tool,request,effect,status) VALUES(?,?,?,?,?,?)",
                (
                    "intent:bilibili:comment:one-comment",
                    "bilibili",
                    "comment",
                    json.dumps(args, sort_keys=True, ensure_ascii=False),
                    "write",
                    "unknown",
                ),
            )
            kernel.store.db.commit()
            result = await kernel.call("bilibili", "comment", args, operation_id="a-new-model-call")
            assert result.status == "unknown"
            assert seen == []
            result = await kernel.call("bilibili", "comment", {**args, "intent_id": "new-comment"})
            assert result.status == "succeeded"
            assert len(seen) == 1
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_legacy_raw_video_result_is_projected_and_replays_with_original_receipt(
    tmp_path, model_server, bili_server
):
    from astcho_world.dsh_runtime import DSHAgent

    script, requests, model = model_server
    backend, _ = bili_server
    script.extend(
        [
            [("world_tools", {"app_id": "bilibili"})],
            [("bilibili__video", {"bvid": BV})],
            [("world_finish", {})],
        ]
    )
    original_info = {
        "bvid": BV,
        "title": "selected-video",
        "pages": [],
        "ugc_season": {
            "title": "large-collection",
            "sections": [
                {
                    "episodes": [
                        {
                            "bvid": BV,
                            "title": "episode",
                            "arc": {"desc": "RAW_COLLECTION_BODY" * 40000},
                        }
                    ]
                }
            ],
        },
    }

    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(BILI, config={"test_backend": backend})
        call = kernel.call

        async def legacy_call(app_id, name, args, **kwargs):
            result = await call(app_id, name, args, **kwargs)
            if app_id == "bilibili" and name == "video":
                return result.model_copy(
                    update={"data": {"info": original_info, "scope": "metadata only"}}
                )
            return result

        kernel.call = legacy_call
        agent = DSHAgent(kernel, model)
        try:
            assert (await agent.run(max_steps=2))["status"] == "budget_exhausted"
            inspection = await agent.inspect_context()
            assert inspection and len(requests) == 2
            assert max(n["characters"] for n in inspection["nodes"]) < 15000
            source = kernel.store.db.execute(
                "SELECT seq,data FROM events WHERE kind='decision.result' "
                "AND json_extract(data,'$.tool')='bilibili__video'"
            ).fetchone()
            original = json.loads(source["data"])["result"]
            assert original["data"]["info"] == original_info
            await agent.close()
            agent = DSHAgent(kernel, model)
            await agent.run()
            tool = next(
                json.loads(m["content"])
                for m in requests[-1]["messages"]
                if m["role"] == "tool" and "original_result_event_seq" in m["content"]
            )
            assert tool["operation_id"] == original["operation_id"]
            assert tool["status"] == original["status"]
            assert tool["data"]["original_result_event_seq"] == source["seq"]
            assert "RAW_COLLECTION_BODY" not in json.dumps(requests[-1])
            assert (
                await agent.tools.execute("world_recall", {"event_seq": source["seq"]}, "read")
            )["items"][0]["data"]["result"] == original
            assert (await agent.inspect_context())["nodes"][-1]["balance"] == 0
            finished = kernel.activities.finished_ids()[-1]
            archive = kernel.activities.archive(finished)
            assert "RAW_COLLECTION_BODY" not in json.dumps(archive["source_groups"])
            assert "RAW_COLLECTION_BODY" in json.dumps(archive["events"])
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())


def test_web_account_control_is_authenticated_and_not_subject_experience(tmp_path, bili_server):
    backend, seen = bili_server
    (tmp_path / "bili.json").write_text(BILI.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(
        json.dumps({"apps": [{"manifest": "bili.json", "config": {"test_backend": backend}}]})
    )
    token = "synthetic-private-dashboard-token"
    app = create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id="10001",
        ingress_token=None,
        web_token=token,
    )
    with TestClient(app) as client:
        url = "/api/apps/bilibili/control"
        assert (
            client.post(
                url, json={"tool": "account", "arguments": {"action": "status"}}
            ).status_code
            == 401
        )
        assert client.post("/api/session", json={"token": token}).status_code == 200
        response = client.post(url, json={"tool": "account", "arguments": {"action": "status"}})
        assert response.status_code == 200
        assert client.post(url, json={"tool": "like", "arguments": {}}).status_code == 403
        assert (
            client.portal.call(
                lambda: app.state.kernel.store.db.execute(
                    "SELECT count(*) FROM observations"
                ).fetchone()[0]
            )
            == 0
        )
        assert len(seen) == 1
