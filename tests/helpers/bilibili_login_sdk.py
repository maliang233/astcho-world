"""Run with the isolated Bili interpreter; real SDK, synthetic HTTP, no network."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from bilibili_api import user

from astcho_world.apps.bilibili import Bilibili
from astcho_world.bilibili_login import WebQRLogin


async def main():
    with tempfile.TemporaryDirectory(prefix="astcho-qr-sdk-") as folder:
        root = Path(folder)
        polls = [86101, 86090, 0]
        visits = []

        def handle(request):
            visits.append(request.url.path)
            if request.url.path.endswith("generate"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "url": "https://passport.bilibili.com/scan?key=synthetic",
                    "qrcode_key": "synthetic-key",
                }})
            if request.url.path.endswith("poll"):
                return httpx.Response(200, json={"code": 0, "data": {
                    "code": polls.pop(0), "refresh_token": "synthetic-refresh",
                    "url": "https://passport.biligame.com/crossDomain?ticket=synthetic-ticket",
                }})
            return httpx.Response(302, headers=[
                ("location", "https://passport.bilibili.com/finish"),
                ("set-cookie", "SESSDATA=synthetic%2Csession; Domain=.bilibili.com; Path=/"),
                ("set-cookie", "bili_jct=synthetic-csrf; Domain=.bilibili.com; Path=/"),
                ("set-cookie", "DedeUserID=12345; Domain=.bilibili.com; Path=/"),
            ])

        transport = httpx.MockTransport(handle)
        login = WebQRLogin(root, transport=transport)
        await login.generate_qrcode()
        assert login.get_qrcode_picture().content.startswith(b"\x89PNG")
        app = Bilibili(root, {})
        app.login = login
        assert (await app.account("check"))["status"] == "scan_required"
        assert (await app.account("check"))["status"] == "confirmation_required"
        original = user.get_self_info
        failures = [True]

        async def verify(*, credential):
            assert credential.sessdata == "synthetic%2Csession"
            assert credential.bili_jct == "synthetic-csrf"
            if failures:
                failures.pop()
                raise ConnectionError("synthetic temporary verification failure")
            return {"mid": 12345, "name": "合成账号"}

        user.get_self_info = verify
        try:
            try:
                await app.account("check")
                raise AssertionError("Expected the synthetic verification outage")
            except ConnectionError:
                pass
            assert not (root / "credential.json").exists()
            app.db.close()
            # A fresh App resumes only candidate validation, not a consumed key.
            app = Bilibili(root, {})
            app.login = WebQRLogin(root, transport=transport)
            assert (await app.account("check"))["logged_in"] is True
            assert not (root / "login-session.json").exists()
            saved = json.loads((root / "credential.json").read_text())
            assert saved["ac_time_value"] == "synthetic-refresh"
            app.db.close()
            app = Bilibili(root, {})
            assert (await app.account("status"))["logged_in"] is True
            assert visits.count("/x/passport-login/web/qrcode/poll") == 3
            assert (await app.account("check", login_id="obsolete"))["status"] == "superseded"
            assert (await app.account("logout"))["logged_in"] is False
            assert not (root / "credential.json").exists()
        finally:
            user.get_self_info = original
            app.db.close()
    print("real SDK QR flow / cross-domain cookies / outage / restart / status / logout passed")


asyncio.run(main())
