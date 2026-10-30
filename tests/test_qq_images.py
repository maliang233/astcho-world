"""Real image HTTP + original VisionService/LLMService, with synthetic content only."""

import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from astcho.prompts import vision_prompt
from astcho.services.llm import LLMService
from astcho.services.vision import VisionService
from astcho_world.apps.qq import MANIFEST, QQ
from astcho_world.images import DETAIL_PROMPT, ImageReader
from tests.test_dsh_runtime import model_server as model_server_fixture
from tests.test_group_subject import incoming, params, setup_group
from tests.test_preamble import preamble

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)
model_server = model_server_fixture


@pytest.fixture
def image_server():
    state = {"gets": [], "requests": [], "expired": False, "model_failure": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["gets"].append(self.path)
            if state["expired"] or self.path == "/broken":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(request)
            if state["model_failure"]:
                body = json.dumps({"error": {"message": "Synthetic vision rejection"}}).encode()
                self.send_response(400)
            else:
                if "response_format" in request:
                    content = json.dumps(
                        {
                            "description": "合成白色图片",
                            "is_sticker": False,
                            "heart_throb": False,
                            "contains_real_person": False,
                            "is_screenshot": False,
                            "tags": [],
                            "inclination": "neutral",
                        }
                    )
                else:
                    question = request["messages"][-1]["content"][0]["text"]
                    content = "合成细看结果：" + question
                body = json.dumps(
                    {
                        "id": "synthetic-vision",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "synthetic-vision",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
                    }
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def wire_vision(runtime, base):
    settings = SimpleNamespace(
        llm_api_key="synthetic",
        llm_base_url=base + "/v1",
        planner_api_key="synthetic",
        planner_base_url=base + "/v1",
        reasoning_api_key="synthetic",
        reasoning_base_url=base + "/v1",
        vision_api_key="synthetic",
        vision_base_url=base + "/v1",
        vision_model="synthetic-vision",
        input_price_per_million=0,
        output_price_per_million=0,
    )
    runtime.vision = VisionService(settings, LLMService(settings), runtime.sqlite)


def add_images(app, base, paths=("/image",), mid="124", chat="group:10003"):
    segments = [{"type": "image", "data": {"url": base + path}} for path in paths]
    app.ingest(
        {
            "batch_id": mid,
            "chat_id": chat,
            "messages": [{**incoming(mid, text=""), "raw_event": {"message": segments}}],
        }
    )


async def opened(app, **arguments):
    return await app.dispatch(
        "call",
        {
            "tool": "read_messages",
            "operation_id": "read",
            "arguments": {"chat_id": "group:10003", **arguments},
        },
    )


async def inspect(app, observation, question="请读出文字", **arguments):
    return await app.dispatch(
        "call",
        {
            "tool": "view_image",
            "operation_id": "inspect",
            "arguments": {
                "observation_id": observation,
                "message_id": "124",
                "question": question,
                **arguments,
            },
        },
    )


def test_only_opening_reads_images_and_original_prompt_is_unchanged(tmp_path, image_server):
    base, state = image_server
    app, runtime, sends, memories, _ = setup_group(tmp_path)
    wire_vision(runtime, base)
    add_images(app, base)
    assert state["gets"] == state["requests"] == []

    async def scenario():
        snapshot = await opened(app)
        message = snapshot["messages"][-1]
        assert message["image_description"] == "合成白色图片"
        assert message["media"][0]["status"] == "understood"
        assert message["media"][0]["retained"] is True
        assert snapshot["requires_media"] is False
        assert "合成白色图片" in snapshot["reply_context"]["decision_context"]
        assert app.snapshot(snapshot["observation_id"])["messages"] == snapshot["messages"]
        assert len(state["requests"]) == 1
        assert state["requests"][0]["messages"][0]["content"][0]["text"] == vision_prompt()
        await opened(app)
        assert len(state["requests"]) == 1  # Original classification cache, not another model call.
        assert len(state["gets"]) == 2  # First retention + unchanged legacy downloader.
        assert sends == memories == []
        assert app.db.execute("select count(*) from participation").fetchone()[0] == 0
        assert runtime.emotion.state("10003", "10002")["affinity"] == 0.3

    asyncio.run(scenario())


def test_detail_reuses_original_after_url_expiry_and_process_restart(tmp_path, image_server):
    base, state = image_server
    app, runtime, sends, memories, _ = setup_group(tmp_path)
    wire_vision(runtime, base)
    add_images(app, base, ("/one", "/two"))

    async def scenario():
        snapshot = await opened(app)
        before = app.snapshot(snapshot["observation_id"])
        gets = len(state["gets"])
        state["expired"] = True
        restarted = QQ(tmp_path, app.config)
        restarted.conversation = app.conversation
        first = await inspect(
            restarted, snapshot["observation_id"], "画面有哪些文字？", image_index=2
        )
        second = await inspect(restarted, snapshot["observation_id"], "再看看角落")
        assert first["status"] == second["status"] == "understood"
        assert "画面有哪些文字？" in first["answer"]
        assert "再看看角落" in second["answer"]
        assert len(state["gets"]) == gets
        assert restarted.snapshot(snapshot["observation_id"]) == before
        detail = state["requests"][-1]
        assert detail["messages"][0] == {"role": "system", "content": DETAIL_PROMPT}
        assert detail["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        assert "response_format" not in detail
        assert sends == memories == []
        restarted.db.close()

    asyncio.run(scenario())


def test_failure_is_per_image_does_not_block_text_reply_or_preamble(tmp_path, image_server):
    base, state = image_server
    app, runtime, sends, _, _ = setup_group(tmp_path)
    wire_vision(runtime, base)
    add_images(app, base, ("/broken", "/good"))

    async def scenario():
        snapshot = await opened(app)
        media = snapshot["messages"][-1]["media"]
        assert [item["status"] for item in media] == ["failed", "understood"]
        assert snapshot["requires_media"] is True
        assert "未理解" in snapshot["messages"][-1]["image_description"]
        await app.dispatch("call", preamble(snapshot["observation_id"]))
        result = await app.dispatch(
            "call", params("reply_group", snapshot["observation_id"], text="回应已知文本")
        )
        assert result["status"] == "completed"
        assert len(sends) == 2
        # The unknown image has not been silently described or treated as a sticker.
        assert "classification" not in media[0]

    asyncio.run(scenario())


def test_failed_model_does_not_poison_description_or_prevent_retry(tmp_path, image_server):
    base, state = image_server
    app, runtime, _, _, _ = setup_group(tmp_path)
    wire_vision(runtime, base)
    add_images(app, base)
    state["model_failure"] = True

    async def scenario():
        first = await opened(app)
        assert first["messages"][-1]["media"][0]["status"] == "failed"
        assert (await inspect(app, first["observation_id"]))["status"] == "failed"
        state["model_failure"] = False
        second = await opened(app)
        assert second["messages"][-1]["media"][0]["status"] == "understood"
        assert len(state["requests"]) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"message_id": "not-observed"},
        {"message_id": "123"},
        {"image_index": 2},
        {"image_index": 0},
    ],
)
def test_image_targets_cannot_escape_snapshot(tmp_path, image_server, arguments):
    base, state = image_server
    app, runtime, _, _, _ = setup_group(tmp_path)
    wire_vision(runtime, base)
    add_images(app, base)
    snapshot = app.read({"chat_id": "group:10003"})
    with pytest.raises(ValueError):
        asyncio.run(inspect(app, snapshot["observation_id"], **arguments))
    assert state["gets"] == state["requests"] == []


def test_read_scope_does_not_understand_later_unread_media(tmp_path, image_server):
    base, state = image_server
    app, runtime, _, _, _ = setup_group(tmp_path)
    wire_vision(runtime, base)
    add_images(app, base)

    async def scenario():
        snapshot = await opened(app, anchor_message_id="123")
        assert len(snapshot["messages"]) == 1
        assert state["gets"] == state["requests"] == []
        with pytest.raises(ValueError, match="observed snapshot"):
            await inspect(app, snapshot["observation_id"])

    asyncio.run(scenario())


def test_missing_source_and_other_media_remain_explicitly_unknown(tmp_path):
    app, _, sends, _, _ = setup_group(tmp_path)
    app.ingest(
        {
            "batch_id": "media",
            "chat_id": "group:10003",
            "messages": [
                {
                    **incoming("124", text=""),
                    "raw_event": {
                        "message": [
                            {"type": "image", "data": {"file": "synthetic.png"}},
                            {"type": "record", "data": {}},
                            {"type": "video", "data": {}},
                            {"type": "forward", "data": {"id": "synthetic"}},
                        ]
                    },
                }
            ],
        }
    )

    async def scenario():
        snapshot = await opened(app)
        assert snapshot["requires_media"] is True
        assert all(m["status"] != "understood" for m in snapshot["messages"][-1]["media"])
        assert (await inspect(app, snapshot["observation_id"]))["status"] == "unavailable"
        assert sends == []

    asyncio.run(scenario())


def test_read_deadline_leaves_media_unknown_instead_of_losing_text(tmp_path, monkeypatch):
    app, _, _, _, _ = setup_group(tmp_path)
    add_images(app, "http://example.invalid")

    class Slow:
        async def brief(self, source):
            await asyncio.sleep(1)

    app._image_reader = Slow()
    monkeypatch.setattr("astcho_world.qq_media.OPEN_MEDIA_SECONDS", 0.01)
    snapshot = asyncio.run(opened(app))
    assert snapshot["messages"][-1]["media"][0]["status"] == "deferred"
    assert snapshot["messages"][0]["text"] == "刚刚的问题"


def test_retention_enforces_size_and_http_sources(tmp_path, image_server, monkeypatch):
    base, _ = image_server
    reader = ImageReader(tmp_path, None)
    monkeypatch.setattr("astcho_world.images.MAX_IMAGE_BYTES", 4)
    with pytest.raises(ValueError, match="asset limit"):
        asyncio.run(reader.retain(base + "/image"))
    with pytest.raises(ValueError, match="HTTP URL"):
        asyncio.run(reader.retain("file:///private/synthetic.png"))


def test_view_schema_only_allows_observed_targets():
    from jsonschema import ValidationError, validate

    schema = MANIFEST.tool("view_image").input_schema
    args = {"observation_id": "observed", "message_id": "123", "question": "细看"}
    validate(args, schema)
    for injected in [{"url": "https://example.invalid"}, {"file_path": "/private/image"}]:
        with pytest.raises(ValidationError):
            validate({**args, **injected}, schema)


def test_real_sdk_receives_brief_then_requests_detail_without_native_image_route(
    tmp_path, monkeypatch, model_server, image_server
):
    from astcho_world.dsh_runtime import DSHAgent
    from astcho_world.kernel import Kernel

    script, requests, model = model_server
    base, vision = image_server
    profile = tmp_path / "profile.json"
    profile.write_text('{"name":"星回"}')
    for key, value in {
        "ASTCHO_LLM_API_KEY": "synthetic",
        "ASTCHO_LLM_BASE_URL": base + "/v1",
        "ASTCHO_CHAT_MODEL": "must-not-run",
        "ASTCHO_PLANNER_MODEL": "must-not-run",
        "ASTCHO_VISION_MODEL": "synthetic-vision",
        "ASTCHO_VISION_API_KEY": "synthetic",
        "ASTCHO_VISION_BASE_URL": base + "/v1",
        "ASTCHO_WORLD_BOT_ID": "10001",
        "ASTCHO_WORLD_QQ_API": base + "/unused",
        "ASTCHO_ADMINS": "10004",
        "ASTCHO_ALLOWED_GROUPS": "10003",
        "ASTCHO_MEMORY_ENABLED": "false",
        "ASTCHO_PROFILE_PATH": str(profile),
    }.items():
        monkeypatch.setenv(key, value)

    def request_detail():
        results = [
            json.loads(m["content"]) for m in requests[-1]["messages"] if m["role"] == "tool"
        ]
        snapshot = next(r["data"] for r in results if "observation_id" in r.get("data", {}))
        assert snapshot["messages"][-1]["media"][0]["status"] == "understood"
        return [
            (
                "qq__view_image",
                {
                    "observation_id": snapshot["observation_id"],
                    "message_id": "124",
                    "image_index": 1,
                    "question": "仔细看看图片",
                },
            )
        ]

    def finish():
        assert "合成细看结果" in json.dumps(requests[-1]["messages"], ensure_ascii=False)
        return [("world_finish", {})]

    script.extend(
        [
            [("world_tools", {"app_id": "qq"})],
            [("qq__read_messages", {"chat_id": "group:10003"})],
            request_detail,
            finish,
        ]
    )

    async def scenario():
        kernel = Kernel(tmp_path / "world")
        kernel.register(MANIFEST, config={"conversation": {"allow_send": True}})
        agent = DSHAgent(kernel, model, profile_path=profile)
        try:
            await kernel.ingest(
                "qq",
                {
                    "batch_id": "synthetic-image",
                    "chat_id": "group:10003",
                    "messages": [
                        {
                            **incoming("124", text=""),
                            "raw_event": {
                                "message": [{"type": "image", "data": {"url": base + "/image"}}]
                            },
                        }
                    ],
                },
            )
            assert vision["requests"] == []
            result = await agent.run()
            assert result["status"] == "idle"
            assert len(vision["requests"]) == 2
            assert all(r["model"] == "synthetic-vision" for r in vision["requests"])
            assert "data:image" not in json.dumps(requests)
            assert all(r["model"] == "world-fixture" for r in requests)
            assert (
                kernel.store.db.execute(
                    "select count(*) from operations where effect='write'"
                ).fetchone()[0]
                == 0
            )
        finally:
            await agent.close()
            await kernel.close()

    asyncio.run(scenario())
    assert len(requests) == 4
