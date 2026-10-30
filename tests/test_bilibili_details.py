import asyncio
import json
from types import SimpleNamespace

import pytest

from astcho_world.apps.bilibili import Bilibili
from astcho_world.bilibili_details import load_details

BV = "BV1xx411c7mD"


def test_video_collection_and_parts_are_paged_without_replaying_raw_bodies(tmp_path):
    entries = [
        {
            "bvid": f"BV{i:010d}",
            "title": f"episode-{i}",
            "arc": {"duration": 60, "desc": "unrequested-collection-body" * 500},
        }
        for i in range(120)
    ]
    info = {
        "bvid": BV,
        "title": "requested-video",
        "desc": "requested-description",
        "pages": [{"page": i, "part": f"part-{i}", "cid": i} for i in range(1, 24)],
        "ugc_season": {
            "title": "collection",
            "sections": [{"title": "section", "episodes": entries}],
        },
    }
    app = Bilibili(tmp_path, {})

    async def get_info():
        return info

    app.video = lambda bvid: SimpleNamespace(get_info=get_info)
    app.load_backend = lambda: None

    async def scenario():
        result = await app.execute("video", {"bvid": BV})
        assert result["info"]["desc"] == "requested-description"
        assert result["collection"]["episodes_count"] == 120
        assert len(result["info"]["pages"]) == 10 and result["parts"]["next_page"] == 2
        assert len(json.dumps(result)) < 4000
        assert "unrequested-collection-body" not in json.dumps(result)
        assert load_details(tmp_path, result["source_ref"], BV) == info

        async def refuse_network():
            raise AssertionError("saved snapshot must not refetch the video")

        app.video = lambda bvid: SimpleNamespace(get_info=refuse_network)
        args = {"bvid": BV, "source_ref": result["source_ref"], "limit": 2}
        collection = await app.execute("collection", args)
        assert [e["title"] for e in collection["items"]] == ["episode-0", "episode-1"]
        assert collection["next_page"] == 2 and collection["total"] == 120
        later = await app.execute("collection", {**args, "page": 60})
        assert [e["title"] for e in later["items"]] == ["episode-118", "episode-119"]
        assert later["next_page"] is None
        parts = await app.execute("video", {**args, "page": 2})
        assert [p["part"] for p in parts["info"]["pages"]] == ["part-3", "part-4"]

        app.db.execute(
            "INSERT INTO browsed(created,tool,data) VALUES(1,'video',?)",
            (json.dumps({"info": info}),),
        )
        app.db.commit()
        history = await app.execute("history", {})
        assert history["items"][0]["data"]["title"] == "requested-video"
        assert "unrequested-collection-body" not in json.dumps(history)
        with pytest.raises(ValueError):
            load_details(tmp_path, result["source_ref"], "BV0000000000")
        with pytest.raises(ValueError):
            load_details(tmp_path, "../outside", BV)

    try:
        asyncio.run(scenario())
    finally:
        app.db.close()
