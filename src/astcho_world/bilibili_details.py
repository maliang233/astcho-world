"""Explicit views of video metadata; collection bodies stay behind a read handle."""

import hashlib
import json
import re
from pathlib import Path

from astcho_world.activities import atomic_json


def retain_details(root, info):
    digest = hashlib.sha256(
        json.dumps(info, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    atomic_json(Path(root) / "video_details" / f"{digest}.json", info)
    return digest


def load_details(root, source_ref, bvid):
    if not re.fullmatch(r"[0-9a-f]{64}", source_ref):
        raise ValueError("Invalid video source reference")
    info = json.loads((Path(root) / "video_details" / f"{source_ref}.json").read_text())
    digest = hashlib.sha256(
        json.dumps(info, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if digest != source_ref or info.get("bvid") != bvid:
        raise ValueError("Video source does not match reference or requested video")
    return info


def page_items(items, page, limit):
    start = (page - 1) * limit
    return {
        "items": items[start : start + limit],
        "page": page,
        "total": len(items),
        "next_page": page + 1 if start + limit < len(items) else None,
    }


def video_details(info, source_ref, *, page=1, limit=10):
    fields = (
        "bvid",
        "aid",
        "title",
        "desc",
        "duration",
        "pic",
        "pubdate",
        "tname",
        "owner",
        "stat",
        "cid",
        "dimension",
        "is_upower_exclusive",
        "is_upower_preview",
    )
    metadata = {k: info[k] for k in fields if k in info}
    parts = page_items(
        [
            {k: p[k] for k in ("page", "cid", "part", "duration") if k in p}
            for p in info.get("pages", [])
        ],
        page,
        limit,
    )
    metadata["pages"] = parts.pop("items")
    season = info.get("ugc_season") or {}
    collection = {k: season[k] for k in ("id", "title", "intro", "cover") if k in season}
    collection["episodes_count"] = sum(
        len(section.get("episodes", [])) for section in season.get("sections", [])
    )
    return {
        "info": metadata,
        "parts": parts,
        "collection": collection if season else None,
        "source_ref": source_ref,
        "scope": "this video's metadata and one page of parts; "
        "collection episodes require collection tool",
    }


def collection_details(info, source_ref, *, page=1, limit=10):
    season = info.get("ugc_season") or {}
    episodes = []
    for section in season.get("sections", []):
        for entry in section.get("episodes", []):
            archive = entry.get("arc") or entry.get("archive") or {}
            item = {k: entry[k] for k in ("id", "bvid", "aid", "title") if k in entry}
            for k in ("bvid", "aid", "title", "duration", "pic"):
                if k not in item and k in archive:
                    item[k] = archive[k]
            item["section"] = section.get("title")
            episodes.append(item)
    return {
        "status": "available" if season else "no_collection",
        "title": season.get("title"),
        **page_items(episodes, page, limit),
        "source_ref": source_ref,
        "scope": "one page of collection candidate metadata; not watched content",
    }


def history_index(result):
    """A browsing list must not replay thirty full subtitle/video/tool responses."""
    info = result.get("info") or {}
    item = {k: result[k] for k in ("bvid", "status", "source_ref", "resource") if k in result}
    item.update({k: info[k] for k in ("bvid", "title") if k in info})
    return item
