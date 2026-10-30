"""Private, resumable WEB QR login; supports ticket-based and legacy responses."""

import base64
import html
import io
import json
import time
import uuid
from enum import Enum
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from astcho_world.activities import atomic_json

PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
COOKIE_FIELDS = {
    "sessdata": "sessdata",
    "bili_jct": "bili_jct",
    "dedeuserid": "dedeuserid",
    "buvid3": "buvid3",
    "buvid4": "buvid4",
}


class State(Enum):
    SCAN = "scan_required"
    CONF = "confirmation_required"
    TIMEOUT = "expired"
    DONE = "verifying"


def trusted_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and not parsed.username
        and parsed.port in (None, 443)
        and (
            host in {"bilibili.com", "biligame.com"}
            or host.endswith(".bilibili.com")
            or host.endswith(".biligame.com")
        )
    )


def render_qr(url):
    import qrcode

    stream = io.BytesIO()
    qrcode.make(url).save(stream, format="PNG")
    return stream.getvalue()


class WebQRLogin:
    def __init__(self, root, *, transport=None):
        self.root = Path(root)
        self.path = self.root / "login-session.json"
        self.transport = transport
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}

    def save(self):
        atomic_json(self.path, self.data)

    def clear(self):
        self.data = {}
        self.path.unlink(missing_ok=True)
        (self.root / "login.png").unlink(missing_ok=True)

    def client(self):
        return httpx.AsyncClient(
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            timeout=12,
            follow_redirects=False,
            transport=self.transport,
        )

    async def request(self, client, url, **kwargs):
        try:
            response = await client.get(url, **kwargs)
            if not response.is_redirect:
                response.raise_for_status()
            return response
        except httpx.HTTPError:
            # Do not include a one-use ticket or QR key from an exception URL.
            raise ValueError("连接B站登录服务失败，请稍后重试；当前扫码进度已保留") from None

    async def generate_qrcode(self):
        async with self.client() as client:
            response = await self.request(
                client, PASSPORT + "generate", params={"source": "main-fe-header"}
            )
            body = response.json()
        data = body.get("data", {})
        if (
            body.get("code") != 0
            or not data.get("qrcode_key")
            or not trusted_url(data.get("url", ""))
        ):
            raise ValueError("B站未返回可用登录二维码，请稍后重试")
        image = render_qr(data["url"])
        self.data = {
            "id": uuid.uuid4().hex,
            "key": data["qrcode_key"],
            "status": "scan_required",
            "created": time.time(),
            "expires_at": time.time() + 180,
        }
        path = self.root / "login.png"
        path.write_bytes(image)
        path.chmod(0o600)
        self.save()

    def get_qrcode_picture(self):
        return SimpleNamespace(content=(self.root / "login.png").read_bytes())

    def public(self):
        if not self.data:
            return {"status": "not_started", "logged_in": False}
        if not (self.data.get("candidate") or self.data.get("handoff")) and (
            time.time() >= self.data.get("expires_at", 0)
        ):
            self.data["status"] = "expired"
        result = {
            "status": self.data["status"],
            "login_id": self.data["id"],
            "expires_at": self.data["expires_at"],
            "logged_in": False,
        }
        path = self.root / "login.png"
        if path.exists() and result["status"] in {"scan_required", "confirmation_required"}:
            result["qrcode_image"] = (
                "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
            )
        return result

    async def check_state(self):
        if self.data.get("candidate"):
            return State.DONE  # The successful QR key is consumed; retry verification, not polling.
        if self.public()["status"] == "expired":
            return State.TIMEOUT
        async with self.client() as client:
            if self.data.get("handoff"):
                await self.resolve(client)
                return State.DONE
            response = await self.request(
                client,
                PASSPORT + "poll",
                params={
                    "qrcode_key": self.data["key"],
                    "source": "main-fe-header",
                },
            )
            body = response.json()
            if body.get("code") != 0 or not isinstance(body.get("data"), dict):
                raise ValueError("B站登录状态查询失败，请稍后重试")
            data = body["data"]
            code = data.get("code")
            waiting = {86101: State.SCAN, 86090: State.CONF, 86038: State.TIMEOUT}
            if code in waiting:
                state = waiting[code]
                self.data["status"] = state.value
                self.save()
                return state
            if code != 0:
                raise ValueError("B站返回了未识别的扫码状态，请稍后重试")
            # Save the one-use ticket before following it, so transient failures
            # do not throw away the only response containing the login handoff.
            self.data.update(
                status="verifying",
                handoff=html.unescape(data.get("url", "")),
                refresh_token=data.get("refresh_token", ""),
            )
            self.save()
            await self.resolve(client)
        return State.DONE

    async def resolve(self, client):
        url = self.data["handoff"]
        fields = self.data.get("pending_cookies", {})
        for _ in range(8):
            for cookie in client.cookies.jar:
                key = COOKIE_FIELDS.get(cookie.name.lower())
                if key and cookie.value:
                    fields[key] = cookie.value
            if fields.get("sessdata") and fields.get("bili_jct"):
                break
            if not trusted_url(url):
                raise ValueError("B站登录返回了异常的跳转地址，请重新扫码")
            query = parse_qs(urlsplit(url).query)
            for name, values in query.items():
                key = COOKIE_FIELDS.get(name.lower())
                if key and values and values[0]:
                    fields[key] = values[0]
            if fields.get("sessdata") and fields.get("bili_jct"):
                break  # Compatible with the old cookie-bearing redirect URL.
            response = await self.request(client, url)
            # Cross-domain Set-Cookie may be excluded from httpx's ordinary jar;
            # parse each header separately as well, only from trusted Bili hosts.
            for header in response.headers.get_list("set-cookie"):
                cookies = SimpleCookie()
                try:
                    cookies.load(header)
                except CookieError:
                    continue
                for name, cookie in cookies.items():
                    key = COOKIE_FIELDS.get(name.lower())
                    if key and cookie.value:
                        fields[key] = cookie.value
            for cookie in client.cookies.jar:
                key = COOKIE_FIELDS.get(cookie.name.lower())
                if key and cookie.value:
                    fields[key] = cookie.value
            self.data["pending_cookies"] = fields
            self.save()
            if fields.get("sessdata") and fields.get("bili_jct"):
                break
            if not response.is_redirect or not response.headers.get("location"):
                break
            url = urljoin(url, response.headers["location"])
        if not fields.get("sessdata") or not fields.get("bili_jct"):
            raise ValueError("扫码已确认，但B站未返回完整登录凭证，请重新扫码")
        fields["ac_time_value"] = self.data.get("refresh_token", "")
        self.data.update(candidate=fields, status="verifying")
        self.save()

    def get_credential(self):
        from bilibili_api import Credential

        return Credential(**self.data["candidate"])
