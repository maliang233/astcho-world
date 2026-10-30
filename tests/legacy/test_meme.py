import asyncio
import hashlib

import pytest

from astcho.domain.models import MemeAnalysis, MemeTasteDecision, VisionResult
from astcho.services.meme import MemeCurator
from astcho.storage.chroma import ChromaStore
from astcho.storage.sqlite import SQLiteStore


def test_curator_enforces_limit(tmp_path):
    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    curator = MemeCurator(sqlite, vectors, limit=1)
    curator.learn(
        file_id="one",
        url="https://example.invalid/1",
        description="one",
        tags=[],
        inclination="neutral",
    )
    curator.learn(
        file_id="two",
        url="https://example.invalid/2",
        description="two",
        tags=[],
        inclination="neutral",
    )
    assert sqlite.meme_count() == vectors.meme_count() == 1


def test_relearning_existing_meme_is_noop_and_keeps_local_file(tmp_path):
    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    curator = MemeCurator(sqlite, vectors, limit=10)
    local_file = tmp_path / "meme.png"
    local_file.write_bytes(b"image")
    sqlite.upsert_meme(
        {
            "file_id": "m1",
            "url": "https://example.invalid/1.png",
            "local_path": str(local_file),
            "description": "saved sticker",
            "tags": ["happy"],
        }
    )

    learned = curator.learn(
        file_id="m1",
        url="https://example.invalid/1.png",
        description="changed description",
        tags=[],
        inclination="neutral",
    )

    assert learned is False
    assert sqlite.get_meme("m1")["local_path"] == str(local_file)
    assert sqlite.get_meme("m1")["description"] == "saved sticker"
    assert local_file.exists()


def test_force_learning_existing_meme_restores_it_on_vector_failure(tmp_path, monkeypatch):
    url = "https://example.invalid/meme"
    file_id = hashlib.sha256(url.encode()).hexdigest()
    media_dir = tmp_path / "memes"
    media_dir.mkdir()
    local_file = media_dir / f"{file_id}.jpg"
    local_file.write_bytes(b"old-image")
    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    sqlite.upsert_meme(
        {
            "file_id": file_id,
            "url": url,
            "local_path": str(local_file),
            "description": "旧描述",
            "tags": ["旧标签"],
        }
    )

    class Vectors:
        def upsert_meme(self, *args):
            raise RuntimeError("vector unavailable")

    class Response:
        content = b"new-image"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return Response()

    monkeypatch.setattr("astcho.services.meme.httpx.AsyncClient", lambda **kwargs: Client())
    curator = MemeCurator(sqlite, Vectors(), limit=10)

    with pytest.raises(RuntimeError, match="vector unavailable"):
        asyncio.run(
            curator.learn_remote(
                url=url, description="新描述", tags=["新标签"], inclination="快乐"
            )
        )

    assert sqlite.get_meme(file_id)["description"] == "旧描述"
    assert local_file.read_bytes() == b"old-image"


def test_curator_last_info_survives_restart(tmp_path):
    path = tmp_path / "test.sqlite3"
    sqlite = SQLiteStore(path)
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    sqlite.upsert_meme(
        {"file_id": "m1", "url": "https://example.invalid/1.png", "description": "sticker"}
    )
    curator = MemeCurator(sqlite, vectors, limit=10)
    curator.mark_used("m1", session_key="group-1")

    restarted = MemeCurator(SQLiteStore(path), vectors, limit=10)
    assert restarted.last_info("group-1")["file_id"] == "m1"
    assert restarted.last_info("group-2") is None
    assert restarted.delete_last("group-1") is True
    assert restarted.last_info("group-1") is None


def test_curator_taste_gate_rejects_ordinary_image(tmp_path):
    calls = []

    class LLM:
        async def json_completion(self, **kwargs):
            calls.append(kwargs)
            return MemeTasteDecision(heart_throb=False, reason="普通截图")

    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    curator = MemeCurator(sqlite, vectors, limit=10, llm=LLM(), model="test")
    accepted = asyncio.run(
        curator.consider_remote(
            url="https://example.invalid/image",
            vision=VisionResult(
                description="普通风景照",
                is_sticker=False,
                heart_throb=False,
                tags=["风景"],
            ),
        )
    )
    assert accepted is False
    assert sqlite.meme_count() == 0
    assert calls == []


def test_curator_passes_profile_to_taste_prompt(tmp_path):
    calls = []

    class LLM:
        async def json_completion(self, **kwargs):
            calls.append(kwargs)
            return MemeTasteDecision(heart_throb=False, reason="不符合个人品味")

    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    curator = MemeCurator(
        sqlite,
        vectors,
        limit=10,
        llm=LLM(),
        model="test",
        vision_service=object(),
        profile={
            "identity": {"name": "星回", "self_cognition": "有主见的少年"},
            "preferences": {"likes": ["音游"], "dislikes": ["鸡汤图"]},
        },
    )
    asyncio.run(
        curator.consider_remote(
            url="https://example.invalid/image",
            vision=VisionResult(
                description="可爱贴纸",
                is_sticker=True,
                heart_throb=True,
                contains_real_person=False,
                is_screenshot=False,
                tags=["治愈"],
                inclination="治愈",
            ),
        )
    )

    prompt = calls[0]["messages"][0]["content"]
    assert calls[0]["max_tokens"] == 512
    assert calls[0]["thinking"] is False
    assert "星回" in prompt
    assert "有主见的少年" in prompt
    assert "音游" in prompt
    assert "鸡汤图" in prompt


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"is_sticker": False}, False),
        ({"heart_throb": False}, False),
        ({"contains_real_person": True}, False),
        ({"is_screenshot": True}, False),
    ],
)
def test_vision_result_learning_gate_fails_closed(overrides, expected):
    values = {
        "description": "精致的虚构角色贴纸",
        "is_sticker": True,
        "heart_throb": True,
        "contains_real_person": False,
        "is_screenshot": False,
    }
    values.update(overrides)

    assert VisionResult(**values).is_meme_learning_candidate is expected


def test_legacy_cached_vision_result_without_new_flags_is_not_learned():
    result = VisionResult.model_validate(
        {"description": "旧缓存里的贴纸", "is_sticker": True, "tags": ["可爱"]}
    )

    assert result.is_meme_learning_candidate is False
    assert result.is_safe_forced_meme is False


def test_auto_learning_runs_taste_then_deep_vision_before_saving(tmp_path):
    calls = []

    class LLM:
        async def json_completion(self, **kwargs):
            calls.append("taste")
            return MemeTasteDecision(heart_throb=True, reason="喜欢")

    class Vision:
        async def analyze_meme(self, url, context="", *, local_path=None):
            calls.append(("deep", url, context))
            return MemeAnalysis(tags=["卖萌"], inclination="治愈", content_desc="猫猫挥手")

    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    curator = MemeCurator(sqlite, vectors, 10, llm=LLM(), vision_service=Vision())

    async def save(**kwargs):
        calls.append(("save", kwargs))
        return True

    curator.learn_remote = save
    accepted = asyncio.run(
        curator.consider_remote(
            url="https://example.invalid/meme",
            vision=VisionResult(
                description="可爱猫猫贴纸",
                is_sticker=True,
                heart_throb=True,
                contains_real_person=False,
                is_screenshot=False,
            ),
            context="群友在打招呼",
        )
    )

    assert accepted is True
    assert calls[0] == "taste"
    assert calls[1] == ("deep", "https://example.invalid/meme", "群友在打招呼")
    assert calls[2][0] == "save"
    assert calls[2][1]["description"] == "猫猫挥手"
    assert calls[2][1]["tags"] == ["卖萌"]


def test_auto_learning_skips_already_saved_url_before_judging(tmp_path):
    class LLM:
        async def json_completion(self, **kwargs):
            raise AssertionError("saved image should not be judged again")

    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    url = "https://example.invalid/meme"
    sqlite.upsert_meme(
        {
            "file_id": hashlib.sha256(url.encode()).hexdigest(),
            "url": url,
            "description": "已收藏",
        }
    )
    curator = MemeCurator(sqlite, vectors, 10, llm=LLM(), vision_service=object())

    assert (
        asyncio.run(
            curator.consider_remote(
                url=url,
                vision=VisionResult(
                    description="可爱贴纸",
                    is_sticker=True,
                    heart_throb=True,
                    contains_real_person=False,
                    is_screenshot=False,
                ),
            )
        )
        is False
    )


def test_admin_force_skips_classification_and_taste(tmp_path):
    calls = []

    class LLM:
        async def json_completion(self, **kwargs):
            raise AssertionError("force must not call the taste judge")

    class Vision:
        async def analyze_meme(self, url, context="", *, local_path=None):
            calls.append(("deep", url, context))
            return MemeAnalysis(tags=["影视"], inclination="吐槽", content_desc="真人影视截图")

    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    curator = MemeCurator(
        sqlite, vectors, limit=10, llm=LLM(), model="test", vision_service=Vision()
    )

    async def save(**kwargs):
        calls.append(("save", kwargs))
        return True

    curator.learn_remote = save
    analysis = asyncio.run(
        curator.force_learn_remote(url="https://example.invalid/meme", context="管理员手动教导")
    )

    assert analysis.content_desc == "真人影视截图"
    assert calls[0] == ("deep", "https://example.invalid/meme", "管理员手动教导")
    assert calls[1][1]["description"] == "真人影视截图"


def test_force_relearn_last_preserves_file_and_usage(tmp_path):
    local_file = tmp_path / "meme.png"
    local_file.write_bytes(b"image")

    class Vision:
        async def analyze_meme(self, url, context="", *, local_path=None):
            assert local_path == str(local_file)
            assert context == "手动学习"
            return MemeAnalysis(tags=["重学"], inclination="快乐", content_desc="新描述")

    sqlite = SQLiteStore(tmp_path / "test.sqlite3")
    vectors = ChromaStore(tmp_path / "chroma", "unused", embedder=lambda _: [1.0, 0.0])
    sqlite.upsert_meme(
        {
            "file_id": "m1",
            "url": "https://example.invalid/meme",
            "local_path": str(local_file),
            "description": "旧描述",
            "tags": ["旧标签"],
            "inclination": "普通",
        }
    )
    curator = MemeCurator(sqlite, vectors, limit=10, vision_service=Vision())
    curator.mark_used("m1", session_key="group-1")

    analysis = asyncio.run(curator.force_relearn_last("group-1"))
    updated = sqlite.get_meme("m1")

    assert analysis.content_desc == "新描述"
    assert updated["description"] == "新描述"
    assert updated["local_path"] == str(local_file)
    assert updated["use_count"] == 1
    assert local_file.exists()
