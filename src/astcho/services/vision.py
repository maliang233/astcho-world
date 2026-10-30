from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path

import httpx
from openai import APIError

from astcho.config import Settings
from astcho.domain.models import MemeAnalysis, VisionResult
from astcho.logging import get_logger, preview
from astcho.prompts import meme_analysis_prompt, vision_prompt
from astcho.services.llm import LLMResponseError, LLMService
from astcho.storage.sqlite import SQLiteStore

logger = get_logger(__name__)


class VisionService:
    def __init__(self, settings: Settings, llm: LLMService, store: SQLiteStore):
        self.settings, self.llm, self.store = settings, llm, store

    async def describe(self, url: str) -> VisionResult:
        key = hashlib.sha256(url.encode()).hexdigest()
        cached = self.store.get_vision(key)
        if cached and cached.get("_classification_version") == 2:
            logger.debug("🖼️ [视觉缓存] 命中 %s", key[:8])
            return VisionResult.model_validate(cached)
        if cached:
            logger.debug("🖼️ [视觉缓存] 旧结果未经安全分类校验，重新识别 %s", key[:8])
        else:
            logger.debug("🖼️ [视觉缓存] 未命中 %s，下载图片", key[:8])
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("图片下载失败: %s", exc)
            return VisionResult(description="[图片]")
        mime = response.headers.get("content-type", "image/jpeg").split(";")[0]
        encoded = base64.b64encode(response.content).decode()
        try:
            result = await self.llm.json_completion(
                client=self.llm.vision_client,
                model=self.settings.vision_model,
                schema=VisionResult,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt()},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{encoded}"},
                            },
                        ],
                    }
                ],
                thinking=False,
            )
        except (APIError, LLMResponseError, ValueError, httpx.HTTPError) as exc:
            logger.warning("图片识别失败，按不可学习图片降级: %s", exc)
            # Provider/account failures may be temporary. Do not cache the fallback,
            # so the same image can be analyzed after service recovery.
            return VisionResult(description="无法可靠识别的图片")
        if not {"contains_real_person", "is_screenshot"} <= result.model_fields_set:
            logger.warning("图片识别缺少真人/截图安全分类，按不可学习图片降级")
            return VisionResult(description="无法可靠识别的图片")
        cache_result = result.model_dump()
        cache_result["_safety_verified"] = True
        cache_result["_classification_version"] = 2
        self.store.set_vision(key, cache_result)
        logger.debug("🖼️ [视觉缓存] 已写入 %s | %s", key[:8], preview(result.description, 80))
        return result

    async def analyze_meme(
        self, url: str, context: str = "", *, local_path: str | None = None
    ) -> MemeAnalysis | None:
        """Second, image-grounded analysis used after acceptance or by admin force."""
        try:
            path = Path(local_path) if local_path else None
            if path is not None and path.is_file():
                data = path.read_bytes()
                mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                data = response.content
                mime = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            if not data or len(data) > 20 * 1024 * 1024:
                return None
            encoded = base64.b64encode(data).decode()
            return await self.llm.json_completion(
                client=self.llm.vision_client,
                model=self.settings.vision_model,
                schema=MemeAnalysis,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": meme_analysis_prompt(context)},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{encoded}"},
                            },
                        ],
                    }
                ],
                max_tokens=500,
                thinking=False,
            )
        except (APIError, LLMResponseError, ValueError, httpx.HTTPError, OSError) as exc:
            logger.warning("表情深度分析失败，跳过学习: %s", exc)
            return None

    async def describe_video(self, url: str, *, file_id: str = "") -> str:
        if not url:
            return "[视频]"
        key = "video:" + (file_id or hashlib.sha256(url.encode()).hexdigest())
        cached = self.store.get_vision(key)
        if cached:
            logger.debug("🎬 [视觉缓存] 视频命中 %s", key[-8:])
            return str(cached.get("description", "[视频]"))
        prompt = (
            "描述这个短视频或 GIF 的主要画面、文字、动作以及在群聊中表达的情绪或梗。控制在100字内。"
        )
        try:
            result = await self.llm.raw_completion(
                client=self.llm.vision_client,
                model=self.settings.vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "video_url", "video_url": {"url": url}},
                        ],
                    }
                ],
                temperature=0.1,
                max_tokens=300,
                thinking=False,
            )
            description = result.strip()[:500] or "[视频]"
        except Exception as exc:
            logger.warning("视频识别失败: %s", exc)
            description = "[视频]"
        self.store.set_vision(key, {"description": description})
        logger.debug("🎬 [视觉] 识别完成: %s", preview(description, 100))
        return description
