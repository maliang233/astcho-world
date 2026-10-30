# Read-only capability subset of c1overzz/dsh-bili-scroll
# Commit 2c7b3f14f525360f8657b102ec453fd8f02baf3a; MIT, see BILI_LICENSE.txt.
# Decorators/workflows/style gates intentionally excluded by World adapter.


import asyncio
import base64
import json
import random
import struct
import sys
from pathlib import Path

from bilibili_api import Credential, comment, homepage, search, video
from mcp.types import ImageContent, TextContent


def _guard(name):
    return ""


async def _throttle():
    try:
        await asyncio.sleep(random.uniform(0.6, 1.6))
    except Exception:
        pass

CRED_FILE = Path(__file__).parent / "bili_credential.json"

def load_credential() -> Credential:
    """从文件加载凭证"""
    if not CRED_FILE.exists():
        return None
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        # ⚠️ 文件写坏一次（上一次写被打断）会让**所有**工具一起 JSONDecodeError：
        #    这里退化成「没登录」，重新扫一次码就能恢复。（2026-10-16 审查修）
        print(f"[bili-mcp] 凭证读不了（{type(e).__name__}: {e}）：{CRED_FILE}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    return Credential(
        sessdata=data.get("sessdata", ""),
        bili_jct=data.get("bili_jct", ""),
        buvid3=data.get("buvid3") or "",
        dedeuserid=data.get("dedeuserid", ""),
    )

def get_cred() -> Credential:
    """获取凭证，未登录时引导用户调用 bili_login"""
    cred = load_credential()
    if not cred:
        raise Exception("未登录B站！请先调用 bili_login 工具获取登录二维码，让用户扫码登录。")
    return cred

async def bili_search(keyword: str, num: int = 10, order: str = "totalrank", page: int = 1) -> str:
    """
    搜索B站视频

    Args:
        keyword: 搜索关键词，如"AI Agent"、"大模型教程"
        num: 返回视频数量，默认10，最大50
        order: 排序方式 totalrank=综合 click=播放量 pubdate=最新 dm=弹幕
        page: 第几页，从 1 开始（一页约 20 条；想多看一屏就翻页）
    
    Returns:
        JSON格式的视频列表，包含标题、BV号、播放量、评论数、UP主等
    """
    await _throttle()
    order_map = {
        "totalrank": search.OrderVideo.TOTALRANK,
        "click": search.OrderVideo.CLICK,
        "pubdate": search.OrderVideo.PUBDATE,
        "dm": search.OrderVideo.DM,
    }
    order_enum = order_map.get(order, search.OrderVideo.TOTALRANK)

    result = await search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=max(1, int(page or 1)),
        order_type=order_enum,
    )

    videos = []
    for item in result.get("result", [])[:num]:
        title = item.get("title", "").replace('<em class="keyword">', "").replace("</em>", "")
        videos.append({
            "bvid": item.get("bvid", ""),
            "aid": item.get("aid", 0),
            "title": title,
            "author": item.get("author", ""),
            "play": item.get("play", 0),
            "review": item.get("review", 0),
            "danmaku": item.get("video_review", 0),
            "duration": item.get("duration", ""),
            "pubdate": item.get("pubdate", 0),
            "description": item.get("description", "")[:200],
        })

    return json.dumps({"keyword": keyword, "count": len(videos), "videos": videos}, ensure_ascii=False)

async def bili_comments(bvid: str, num: int = 30) -> str:
    """
    获取B站视频的热门评论

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"
        num: 获取评论数量，默认30
    
    Returns:
        JSON格式的评论列表，包含用户名、评论内容、点赞数、回复数
    """
    await _throttle()
    cred = get_cred()
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]

    comments = []
    page = 1
    fetch_error = None
    while len(comments) < num:
        try:
            resp = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.LIKE,
                credential=cred,
            )
            replies = resp.get("replies") or []
            if not replies:
                break

            for r in replies:
                member = r.get("member", {})
                content = r.get("content", {})
                c = {
                    "rpid": r.get("rpid", 0),
                    "user": member.get("uname", ""),
                    "content": content.get("message", ""),
                    "like": r.get("like", 0),
                    "reply_count": r.get("rcount", 0),
                    "time": r.get("ctime", 0),
                }
                # 子评论
                sub_replies = []
                for sub in (r.get("replies") or [])[:2]:
                    sub_replies.append({
                        "user": sub.get("member", {}).get("uname", ""),
                        "content": sub.get("content", {}).get("message", ""),
                        "like": sub.get("like", 0),
                    })
                if sub_replies:
                    c["top_replies"] = sub_replies
                comments.append(c)

            page += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            # ⚠️ 抓取失败（风控 -412 / 网络错）不能吞成「这条视频 0 条评论」：
            #    调用方必须能分辨「真没评论」和「没抓到」，否则日报静默失真。（2026-10-16 审查修）
            fetch_error = f"{type(e).__name__}: {e}"
            break

    out = {"bvid": bvid, "count": len(comments[:num]), "comments": comments[:num]}
    if fetch_error:
        out["error"] = fetch_error
    return json.dumps(out, ensure_ascii=False)

_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_COVER_WIDTH = 512

_HTTP_TIMEOUT = 25

_MAX_IMAGE_BYTES = 1_500_000

def _https(url: str) -> str:
    """把 B 站的图片/资源地址统一成 https。

    view 接口给的 `pic` 实测 10/10 都是 `http://`，而 http 不会跳转 https；
    图床请求再带上 SESSDATA 就等于把登录凭据明文发出去。
    """
    u = str(url or "")
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://"):
        return "https://" + u[len("http://"):]
    return u

def _cover_url(pic: str, width: int = _COVER_WIDTH) -> str:
    """封面原图地址 → 带 CDN 缩放后缀（@<宽>w.jpg）的地址，等比不裁切。"""
    base = _https(str(pic).split("@", 1)[0])
    return f"{base}@{width}w.jpg"

def _image_headers() -> dict:
    """图床请求头：只给 UA + Referer，**不带 Cookie**（图床不需要登录态）。"""
    return {"User-Agent": _WEB_UA, "Referer": "https://www.bilibili.com/"}

def _text_only(payload: dict) -> list:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

async def bili_cover(bvid: str) -> list:
    """
    看这个视频的封面（等比缩到 512px 宽）

    最便宜的一档"看一眼"：能看出风格、标题美术、大致题材，但看不出具体发生了什么。

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"

    Returns:
        封面图 + 一句说明（标题、时长、图多大）。
        图能不能真被看见取决于当前模型是否支持图片输入；不支持时会被降级成文字。
    """
    import aiohttp

    cred = load_credential()          # 封面是公开的，没登录也取得到
    try:
        v = video.Video(bvid=bvid, credential=cred)   # 格式不对时**构造时就抛**
        info = await v.get_info()
    except Exception as e:
        # ⚠️ 2026-10-23 实测两种失败：`62002 稿件不可见`（BV 不存在）
        #    和 `ArgsException`（格式不对，构造时就抛）。都收成 ok:false。
        return _text_only({"bvid": bvid, "ok": False, "error": f"取视频信息失败：{e}"})

    pic = str(info.get("pic") or "")
    if not pic:
        return _text_only({"bvid": bvid, "ok": False, "error": "这个视频没有封面地址"})

    url = _cover_url(pic)
    try:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=_image_headers()) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                raw = await resp.read()
                mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    except Exception as e:
        return _text_only({"bvid": bvid, "ok": False, "error": f"下载封面失败：{e}"})

    allowed = ("image/jpeg", "image/png", "image/webp", "image/gif")
    if mime not in allowed:
        return _text_only({"bvid": bvid, "ok": False, "error": f"封面不是可用的图片类型：{mime}"})
    if len(raw) > _MAX_IMAGE_BYTES:
        return _text_only({
            "bvid": bvid, "ok": False,
            "error": f"封面太大（{len(raw) // 1024}KB），没放进来",
        })

    return [
        TextContent(type="text", text=json.dumps({
            "bvid": bvid,
            "ok": True,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "cover_kb": round(len(raw) / 1024, 1),
        }, ensure_ascii=False)),
        ImageContent(
            type="image",
            data=base64.b64encode(raw).decode("ascii"),
            mimeType=mime,
        ),
    ]

_FRAME_MAX = 6

_TILE_W_DEFAULT = 480

_TILE_H_DEFAULT = 270

def _snapshot_entries(data: dict, duration: float = 0.0) -> list:
    """把 videoshot 的 index 归一成 [(秒, 块序号), ...]。

    时间单位实测两种都出现过（秒 / 毫秒），只能靠和时长比较来判断：
    超过时长 1.5 倍才当毫秒。⚠️ 别再加"绝对值大于某个数就当毫秒"这类判据 ——
    6 小时视频的秒数（22163）会被误判成毫秒，三帧全塌到同一块、时间缩小 1000 倍。
    """
    raws = []
    for i, item in enumerate(data.get("index") or []):
        ts = item.get("timestamp") if isinstance(item, dict) else item
        try:
            raws.append((float(ts), i))
        except (TypeError, ValueError):
            continue
    if not raws:
        return []

    biggest = max(v for v, _ in raws)
    looks_ms = duration > 0 and biggest > duration * 1.5
    scale = 1000.0 if looks_ms else 1.0
    return [(v / scale, i) for v, i in raws]

async def _entries_from_pvdata(data: dict) -> list:
    """index 为空时的兜底：pvdata 指向一个大端 uint16 秒数数组，与 index 逐项相同。"""
    url = _https(data.get("pvdata") or "")
    if not url:
        return []
    try:
        raw = await _fetch_bytes(url, _image_headers())
    except Exception:
        return []
    if not raw or len(raw) % 2:
        return []
    count = len(raw) // 2
    if count > 20000:            # 明显不对，别乱解
        return []
    return [(float(sec), i) for i, sec in enumerate(struct.unpack(f">{count}H", raw))]

async def _fetch_bytes(url: str, headers: dict) -> bytes:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(_https(url), headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.read()

def _crop_tile(sheet: bytes, tile_index: int, tw: int, th: int) -> bytes:
    """从拼图里裁出第 tile_index 块，返回 jpeg 字节。"""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(sheet))
    cols = max(1, img.width // tw)
    rows = max(1, img.height // th)
    if tile_index >= cols * rows:
        raise RuntimeError(f"第 {tile_index} 块超出拼图（只有 {cols}×{rows}）")
    col, row = tile_index % cols, tile_index // cols
    tile = img.crop((col * tw, row * th, (col + 1) * tw, (row + 1) * th)).convert("RGB")
    buf = io.BytesIO()
    tile.save(buf, format="JPEG", quality=85)
    return buf.getvalue()

async def bili_frames(bvid: str, num: int = 3, at: list | None = None) -> list:
    """
    看这个视频里的画面（从 B 站自带的故事板里裁几帧）

    想看"它到底拍了什么"就用它。默认按时长均匀取 3 帧；
    想盯某个时刻（比如字幕里提到的那一段）就传 at（单位：秒），会取**最接近的那一块**。

    Args:
        bvid: 视频BV号，如"BV1uNk1YxEJQ"
        num: 取几帧，1-6，默认 3
        at: 指定时间点（秒）的列表；传了就以它为准，忽略 num

    Returns:
        若干张 480×270 的画面 + 一句说明（时长、实际取到的那一刻、哪几帧失败）。
        失败会写进 failed。**多P 视频的故事板通常只覆盖第 1P**，返回里会用
        covered_sec / pages 标明实际覆盖范围。图能否被看见取决于模型是否支持图片输入。
    """
    cred = load_credential()          # 故事板不登录也能取
    try:
        v = video.Video(bvid=bvid, credential=cred)
        info = await v.get_info()
    except Exception as e:
        return _text_only({"bvid": bvid, "ok": False, "error": f"取视频信息失败：{e}"})
    duration = float(info.get("duration") or 0)
    pages = info.get("pages") or []
    cid = info.get("cid") or ((pages or [{}])[0].get("cid") or 0)
    if not cid:
        return _text_only({"bvid": bvid, "ok": False, "error": "拿不到 cid"})

    # 故事板只覆盖第 1P：目标时间点按"实际覆盖的长度"算，别落到没采样的 P2 上
    covered = float(pages[0].get("duration") or duration) if pages else duration
    covered = covered or duration

    # index 会偶发返回空（实测约 25%）：先用 pvdata 兜底，两个都空才重试
    snap = {}
    entries = []
    last_err = None
    for attempt in range(2):
        try:
            snap = await v.get_video_snapshot(cid=cid, json_index=True, pvideo=False) or {}
            last_err = None
        except Exception as e:
            snap, last_err = {}, e
        entries = _snapshot_entries(snap, covered)
        if not entries:
            entries = await _entries_from_pvdata(snap)
        if entries and (snap.get("image") or []):
            break
        if attempt < 1:
            await asyncio.sleep(1.0)

    sheets = snap.get("image") or []
    if not entries or not sheets:
        err = f"（最后一次错误：{last_err}）" if last_err is not None else ""
        return _text_only({
            "bvid": bvid, "ok": False,
            "error": f"取不到画面快照（试了 2 次）{err}",
        })

    # 覆盖长度以**故事板实测到的那一刻**为准（比 P1 的标称时长保守，通常短 1–10%）
    story_max = max((e[0] for e in entries), default=0.0)
    if story_max > 0:
        covered = min(covered, story_max)

    # 目标时间点：传了 at 就用它；否则按"覆盖长度"均分
    if at:
        targets = [min(max(0.0, float(t)), max(0.0, covered - 0.1)) for t in at][:_FRAME_MAX]
    else:
        n = max(1, min(int(num or 3), _FRAME_MAX))
        targets = [covered * (i + 1) / (n + 1) for i in range(n)]

    picked = []
    for t in targets:
        ts, tile = min(entries, key=lambda e: abs(e[0] - t))
        picked.append((round(t, 1), round(ts, 1), tile))

    tw = int(snap.get("img_x_size") or _TILE_W_DEFAULT)
    th = int(snap.get("img_y_size") or _TILE_H_DEFAULT)
    headers = _image_headers()

    cache = {}

    async def get_sheet(no: int) -> bytes:
        if no not in cache:
            if no >= len(sheets):
                raise RuntimeError(f"拼图 {no} 不存在（共 {len(sheets)} 张）")
            cache[no] = await _fetch_bytes(str(sheets[no]), headers)
        return cache[no]

    # 每张拼图装几块要等第一张下来才知道（列数 = 宽 // 块宽）
    blocks, frames, failed = [], [], []
    cap = None
    for i, (want, got, tile) in enumerate(picked):
        try:
            if cap is None:
                first = await get_sheet(0)
                import io

                from PIL import Image

                im = Image.open(io.BytesIO(first))
                cap = max(1, (im.width // tw) * (im.height // th))
            sheet_no, pos = tile // cap, tile % cap
            raw = _crop_tile(await get_sheet(sheet_no), pos, tw, th)
            if len(raw) > _MAX_IMAGE_BYTES:
                raise RuntimeError(f"这一帧太大（{len(raw) // 1024}KB）")
            blocks.append(ImageContent(
                type="image",
                data=base64.b64encode(raw).decode("ascii"),
                mimeType="image/jpeg",
            ))
            frames.append({"index": i, "want_sec": want, "got_sec": got,
                           "kb": round(len(raw) / 1024, 1)})
        except Exception as e:
            failed.append({"index": i, "want_sec": want, "got_sec": got, "reason": str(e)})

    if not blocks:
        return _text_only({
            "bvid": bvid, "ok": False,
            "error": "一帧都没取到", "failed": failed,
        })

    return [
        TextContent(type="text", text=json.dumps({
            "bvid": bvid,
            "ok": True,
            "title": info.get("title"),
            "duration": duration,
            "covered_sec": round(covered, 1),
            "pages": max(1, len(pages)),
            "tile": f"{tw}x{th}",
            "frames": frames,
            "failed": failed,        # 空列表 = 全成
            "note": "want_sec 是想要的时刻，got_sec 是实际取到的那一块（故事板按固定间隔切）；"
                    "多P 视频只覆盖第 1P，看 covered_sec",
        }, ensure_ascii=False)),
        *blocks,
    ]

async def bili_recommend(ps: int = 20) -> str:
    """
    获取「首页推荐流」的视频（按当前登录账号的画像）

    Args:
        ps: 返回几条，默认 20，最多 40

    Returns:
        JSON 格式的视频列表，字段与 bili_search 对齐（bvid / title / author / play / danmaku / duration…）
    """
    await _throttle()
    denied = _guard("bili_recommend")
    if denied:
        return json.dumps({"ok": False, "error": denied}, ensure_ascii=False)

    cred = None
    try:
        cred = load_credential()
    except Exception:
        cred = None

    try:
        data = await homepage.get_videos(credential=cred)
    except Exception as e:  # noqa: BLE001 - 统一收成 ok:false，别把原始异常甩给模型
        return json.dumps({"ok": False, "error": f"取推荐流失败：{e}"}, ensure_ascii=False)

    items = []
    if isinstance(data, dict):
        items = data.get("item") or data.get("data") or []
    elif isinstance(data, list):
        items = data

    videos = []
    for it in items[: max(1, min(int(ps), 40))]:
        if not isinstance(it, dict):
            continue
        # 只留视频（推荐流里会混进直播 / 番剧 / 广告卡片）
        if it.get("goto") and it.get("goto") != "av":
            continue
        if not it.get("bvid"):
            continue
        owner = it.get("owner") or {}
        stat = it.get("stat") or {}
        dur = it.get("duration")
        videos.append({
            "bvid": it.get("bvid", ""),
            "title": str(it.get("title", "")).strip(),
            "author": owner.get("name", ""),
            "play": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "like": stat.get("like", 0),
            "duration": f"{int(dur) // 60}:{int(dur) % 60:02d}" if isinstance(dur, (int, float)) else str(dur or ""),
            "pubdate": it.get("pubdate", 0),
            "reason": (it.get("rcmd_reason") or {}).get("content", "") if isinstance(it.get("rcmd_reason"), dict) else "",
        })

    return json.dumps({
        "ok": True,
        "source": "recommend",
        "count": len(videos),
        "videos": videos,
    }, ensure_ascii=False)
