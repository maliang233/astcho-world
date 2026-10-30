import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from astcho_world.bilibili_login import State, WebQRLogin

PNG = b"synthetic-qr-image"


def test_isolated_bilibili_sdk_full_login_and_restart():
    root = Path(__file__).resolve().parents[1]
    runtime = root / "var/runtimes/bilibili/bin/python"
    if not runtime.exists():
        pytest.skip("Optional isolated Bilibili runtime is not installed")
    result = subprocess.run(
        [str(runtime), str(root / "tests/helpers/bilibili_login_sdk.py")],
        cwd=root, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "real SDK QR flow" in result.stdout


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr("astcho_world.bilibili_login.render_qr", lambda url: PNG)
    polls, visits = [], []

    def serve(request):
        visits.append(request)
        if request.url.path.endswith("generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/login/scan?key=synthetic",
                        "qrcode_key": "synthetic-key",
                    },
                },
            )
        if request.url.path.endswith("poll"):
            body = polls.pop(0)
            return httpx.Response(200, json={"code": 0, "data": body})
        if request.url.host == "passport.biligame.com":
            return httpx.Response(
                302,
                headers=[
                    ("Location", "https://passport.bilibili.com/finish"),
                    (
                        "Set-Cookie",
                        "SESSDATA=synthetic%2Csession; Domain=.bilibili.com; Path=/; HttpOnly",
                    ),
                    ("Set-Cookie", "bili_jct=synthetic-csrf; Domain=.bilibili.com; Path=/"),
                    ("Set-Cookie", "DedeUserID=12345; Domain=.bilibili.com; Path=/"),
                ],
            )
        raise AssertionError("Unexpected network destination")

    transport = httpx.MockTransport(serve)
    return WebQRLogin(tmp_path, transport=transport), polls, visits, transport


@pytest.mark.parametrize("modern", [False, True])
def test_old_and_new_cookie_formats_with_real_httpx_cookie_handling(flow, modern):
    login, polls, visits, _ = flow

    async def scenario():
        await login.generate_qrcode()
        assert login.public()["qrcode_image"].startswith("data:image/png;base64,")
        polls.extend(
            [
                {"code": 86101},
                {"code": 86090},
                {
                    "code": 0,
                    "refresh_token": "synthetic-refresh",
                    "url": "https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=one-use"
                    if modern
                    else "https://passport.biligame.com/crossDomain?"
                    "SESSDATA=synthetic%2Csession&bili_jct=synthetic-csrf&DedeUserID=12345",
                },
            ]
        )
        assert await login.check_state() == State.SCAN
        assert await login.check_state() == State.CONF
        assert login.public()["status"] == "confirmation_required"
        assert await login.check_state() == State.DONE
        candidate = login.data["candidate"]
        assert candidate["bili_jct"] == "synthetic-csrf"
        assert candidate["dedeuserid"] == "12345"
        assert candidate["ac_time_value"] == "synthetic-refresh"
        assert "one-use" not in json.dumps(login.public())
        assert "synthetic-csrf" not in json.dumps(login.public())
        count = len(visits)
        assert await login.check_state() == State.DONE
        assert len(visits) == count  # Successful key must never be polled again.
        if modern:
            assert "Mozilla/5.0" in visits[-1].headers["User-Agent"]

    asyncio.run(scenario())


def test_restart_keeps_qr_and_candidate_without_a_second_success_poll(flow):
    login, polls, visits, transport = flow

    async def scenario():
        await login.generate_qrcode()
        public = login.public()
        restored = WebQRLogin(login.root, transport=transport)
        assert restored.public() == public
        polls.append(
            {
                "code": 0,
                "refresh_token": "synthetic-refresh",
                "url": "https://passport.biligame.com/crossDomain?ticket=one-use",
            }
        )
        assert await restored.check_state() == State.DONE
        count = len(visits)
        resumed = WebQRLogin(login.root, transport=transport)
        assert await resumed.check_state() == State.DONE
        assert len(visits) == count
        assert resumed.path.stat().st_mode & 0o777 == 0o600
        resumed.clear()
        assert not resumed.path.exists() and not (login.root / "login.png").exists()

    asyncio.run(scenario())


def test_transient_ticket_failure_resumes_ticket_not_consumed_qr_key(flow):
    login, polls, visits, transport = flow

    async def scenario():
        await login.generate_qrcode()
        polls.append({"code": 0, "url": "https://passport.biligame.com/crossDomain?ticket=one-use"})
        original = login.request

        async def fail_ticket(client, url, **kwargs):
            if "crossDomain" in url:
                raise ValueError("synthetic temporary failure")
            return await original(client, url, **kwargs)

        login.request = fail_ticket
        with pytest.raises(ValueError):
            await login.check_state()
        assert login.data["handoff"]
        resumed = WebQRLogin(login.root, transport=transport)
        assert await resumed.check_state() == State.DONE
        assert sum(r.url.path.endswith("poll") for r in visits) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("code,expected", [(86038, State.TIMEOUT), (123, None)])
def test_expiry_and_unrecognized_codes_are_not_success(flow, code, expected):
    login, polls, _, _ = flow

    async def scenario():
        await login.generate_qrcode()
        polls.append({"code": code})
        if expected:
            assert await login.check_state() == expected
            assert "qrcode_image" not in login.public()
        else:
            with pytest.raises(ValueError, match="未识别"):
                await login.check_state()
        assert not login.data.get("candidate")

    asyncio.run(scenario())


def test_external_redirect_is_rejected_and_network_errors_hide_tickets(flow):
    login, polls, visits, _ = flow

    async def scenario():
        await login.generate_qrcode()
        polls.append({"code": 0, "url": "https://attacker.invalid/?ticket=private-ticket"})
        with pytest.raises(ValueError, match="异常的跳转"):
            await login.check_state()
        assert all(r.url.host != "attacker.invalid" for r in visits)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as client:
            with pytest.raises(ValueError) as error:
                await login.request(client, "https://passport.biligame.com/?ticket=private-ticket")
            assert "private-ticket" not in str(error.value)

    asyncio.run(scenario())
