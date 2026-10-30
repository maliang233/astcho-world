import asyncio
from types import SimpleNamespace

import httpx
from openai import BadRequestError

from astcho.domain.models import MemeAnalysis, VisionResult
from astcho.services.vision import VisionService


def test_deep_meme_analysis_uses_image_and_context(tmp_path):
    local_file = tmp_path / "meme.png"
    local_file.write_bytes(b"image-bytes")
    calls = []

    class LLM:
        vision_client = object()

        async def json_completion(self, **kwargs):
            calls.append(kwargs)
            return MemeAnalysis(tags=["卖萌"], inclination="治愈", content_desc="猫猫挥手")

    service = VisionService(SimpleNamespace(vision_model="vision"), LLM(), object())
    analysis = asyncio.run(
        service.analyze_meme(
            "https://example.invalid/expired", "群友打招呼", local_path=str(local_file)
        )
    )

    assert analysis.tags == ["卖萌"]
    assert calls[0]["schema"] is MemeAnalysis
    content = calls[0]["messages"][0]["content"]
    assert "群友打招呼" in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_api_error_fails_closed_without_caching(monkeypatch):
    class Response:
        content = b"image-bytes"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return Response()

    class LLM:
        vision_client = object()

        async def json_completion(self, **kwargs):
            request = httpx.Request("POST", "https://dashscope.example/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise BadRequestError(
                "Access denied: Arrearage",
                response=response,
                body={"error": {"code": "Arrearage"}},
            )

    class Store:
        def __init__(self):
            self.saved = []

        def get_vision(self, key):
            return None

        def set_vision(self, key, value):
            self.saved.append((key, value))

    monkeypatch.setattr("astcho.services.vision.httpx.AsyncClient", lambda **kwargs: Client())
    store = Store()
    service = VisionService(SimpleNamespace(vision_model="vision"), LLM(), store)

    result = asyncio.run(service.describe("https://example.test/image.png"))

    assert result.description == "无法可靠识别的图片"
    assert result.is_meme_learning_candidate is False
    assert store.saved == []


def test_old_vision_cache_is_rechecked_before_admin_force(monkeypatch):
    class Response:
        content = b"image-bytes"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return Response()

    class Store:
        def __init__(self):
            self.cached = {
                "description": "旧结果误判为安全",
                "is_sticker": True,
                "contains_real_person": False,
                "is_screenshot": False,
            }
            self.saved = None

        def get_vision(self, key):
            return self.cached

        def set_vision(self, key, value):
            self.saved = value

    class LLM:
        vision_client = object()

        def __init__(self):
            self.calls = 0

        async def json_completion(self, **kwargs):
            self.calls += 1
            return VisionResult(
                description="真人照片",
                is_sticker=False,
                heart_throb=False,
                contains_real_person=True,
                is_screenshot=False,
            )

    monkeypatch.setattr("astcho.services.vision.httpx.AsyncClient", lambda **kwargs: Client())
    store = Store()
    llm = LLM()
    result = asyncio.run(
        VisionService(SimpleNamespace(vision_model="vision"), llm, store).describe(
            "https://example.test/image.png"
        )
    )

    assert llm.calls == 1
    assert result.is_safe_forced_meme is False
    assert store.saved["_safety_verified"] is True
    assert store.saved["contains_real_person"] is True


def test_incomplete_visual_safety_result_is_not_cached(monkeypatch):
    class Response:
        content = b"image-bytes"
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return Response()

    class Store:
        def __init__(self):
            self.saved = []

        def get_vision(self, key):
            return None

        def set_vision(self, key, value):
            self.saved.append(value)

    class LLM:
        vision_client = object()

        async def json_completion(self, **kwargs):
            return VisionResult(description="缺少安全字段的图片", is_sticker=True, heart_throb=True)

    monkeypatch.setattr("astcho.services.vision.httpx.AsyncClient", lambda **kwargs: Client())
    store = Store()
    result = asyncio.run(
        VisionService(SimpleNamespace(vision_model="vision"), LLM(), store).describe(
            "https://example.test/image.png"
        )
    )

    assert result.is_safe_forced_meme is False
    assert result.is_meme_learning_candidate is False
    assert store.saved == []
