import asyncio
import json
import sys
from enum import Enum
from types import SimpleNamespace

import pytest

from astcho_world.apps.bilibili import MANIFEST, Bilibili
from astcho_world.kernel import Kernel


@pytest.mark.parametrize(
    "body", [None, "{}", "[]", "broken", '{"sessdata":null}', '{"sessdata":" "}']
)
def test_missing_or_empty_credentials_are_a_normal_status_without_loading_sdk(tmp_path, body):
    if body is not None:
        (tmp_path / "credential.json").write_text(body)
    app = Bilibili(tmp_path, {})

    def forbidden():
        raise AssertionError("No session: must not load SDK or query account")

    app.load_backend = forbidden
    try:
        assert app.ambient_state()["logged_in"] is False
        assert app.credential() is None
        result = asyncio.run(app.dispatch("call", {
            "tool": "account", "operation_id": "status", "arguments": {"action": "status"},
        }))
        assert result == {
            "status": "not_logged_in", "logged_in": False, "reason": "missing_session",
        }
        assert app.db.execute("SELECT count(*) FROM browsed").fetchone()[0] == 0
        if body is not None:
            assert (tmp_path / "credential.json").read_text() == body
    finally:
        app.db.close()


def test_empty_session_through_worker_returns_succeeded_not_unknown(tmp_path):
    async def scenario():
        kernel = Kernel(tmp_path)
        kernel.register(MANIFEST)
        root = tmp_path / "apps" / "bilibili"
        root.mkdir(parents=True)
        (root / "credential.json").write_text('{"sessdata":"","bili_jct":""}')
        try:
            result = await kernel.call("bilibili", "account", {"action": "status"})
            assert result.status == "succeeded" and result.error is None
            assert result.data["logged_in"] is False
            assert result.data["reason"] == "missing_session"
            assert kernel.apps["bilibili"].ambient["data"]["logged_in"] is False
        finally:
            await kernel.close()

    asyncio.run(scenario())


@pytest.fixture
def login_sdk(monkeypatch):
    class ResponseCodeException(Exception):
        def __init__(self, code):
            self.code = code

    class Events(Enum):
        DONE = "done"
        SCAN = "scan"
        TIMEOUT = "timeout"

    class Credential:
        sessdata = "synthetic-session"
        bili_jct = "synthetic-csrf"
        buvid3 = None
        dedeuserid = ""

        def has_sessdata(self):
            return bool(self.sessdata)

        def has_bili_jct(self):
            return bool(self.bili_jct)

    requests = []

    async def get_self_info(*, credential):
        requests.append(credential)
        return {"mid": 12345, "name": "合成账号"}

    user = SimpleNamespace(get_self_info=get_self_info)
    monkeypatch.setitem(sys.modules, "bilibili_api", SimpleNamespace(user=user))
    monkeypatch.setitem(
        sys.modules, "bilibili_api.login_v2", SimpleNamespace(QrCodeLoginEvents=Events)
    )
    monkeypatch.setitem(sys.modules, "bilibili_api.exceptions", SimpleNamespace(
        ResponseCodeException=ResponseCodeException,
    ))
    return Credential, user, ResponseCodeException, Events, requests


def attach_login(app, credential, events):
    async def check_state():
        return events.DONE

    app.login = SimpleNamespace(check_state=check_state, get_credential=lambda: credential)


@pytest.mark.parametrize("field", ["sessdata", "bili_jct"])
def test_qr_done_with_incomplete_cookies_preserves_existing_session(tmp_path, login_sdk, field):
    credential_type, _, _, events, requests = login_sdk
    original = '{"sessdata":"existing-session","bili_jct":"existing-csrf"}'
    (tmp_path / "credential.json").write_text(original)
    app = Bilibili(tmp_path, {})
    credential = credential_type()
    setattr(credential, field, "")
    attach_login(app, credential, events)
    try:
        result = asyncio.run(app.account("check"))
        assert result["status"] == "login_failed"
        assert result["reason"] == "incomplete_login_credential"
        assert requests == [] and app.login is None
        assert (tmp_path / "credential.json").read_text() == original
    finally:
        app.db.close()


def test_qr_login_verifies_account_before_saving_and_recovers_uid(tmp_path, login_sdk):
    credential_type, _, _, events, requests = login_sdk
    app = Bilibili(tmp_path, {})
    credential = credential_type()
    attach_login(app, credential, events)
    try:
        assert asyncio.run(app.account("check")) == {
            "status": "logged_in", "logged_in": True, "name": "合成账号", "uid": 12345,
        }
        saved = json.loads((tmp_path / "credential.json").read_text())
        assert saved["sessdata"] == credential.sessdata
        assert saved["dedeuserid"] == "12345" and requests == [credential]
        assert app.login is None
        assert (tmp_path / "credential.json").stat().st_mode & 0o777 == 0o600
    finally:
        app.db.close()


@pytest.mark.parametrize("action", ["status", "check"])
def test_rejected_session_does_not_become_unknown_or_successful_login(tmp_path, login_sdk, action):
    credential_type, user, error_type, events, _ = login_sdk
    app = Bilibili(tmp_path, {})
    credential = credential_type()

    async def rejected(**kwargs):
        raise error_type(-101)

    user.get_self_info = rejected
    app.credential = lambda: credential
    if action == "check":
        attach_login(app, credential, events)
    try:
        result = asyncio.run(app.account(action))
        assert result["logged_in"] is False
        assert result["reason"] == ("session_rejected" if action == "check" else "session_expired")
        assert not (tmp_path / "credential.json").exists()
    finally:
        app.db.close()


def test_network_failure_is_not_misreported_as_logout(tmp_path, login_sdk):
    credential_type, user, _, _, _ = login_sdk
    app = Bilibili(tmp_path, {})
    app.credential = credential_type

    async def disconnected(**kwargs):
        raise ConnectionError("synthetic network error")

    user.get_self_info = disconnected
    try:
        with pytest.raises(ConnectionError):
            asyncio.run(app.account("status"))
    finally:
        app.db.close()
