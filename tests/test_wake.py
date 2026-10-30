import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from astcho_world.apps.qq import MANIFEST
from astcho_world.host import create_app
from astcho_world.wake import IdleClock, interval
from tests.test_dsh_runtime import model_server as model_server_fixture
from tests.test_reverse_ws import event

model_server = model_server_fixture


@pytest.mark.parametrize(
    "minimum,maximum", [(0, 600), (-1, 600), (600, 120), (float("nan"), 600), (120, float("inf"))]
)
def test_invalid_periodic_range_is_rejected(minimum, maximum):
    with pytest.raises(ValueError, match="finite"):
        IdleClock(0, adaptive=True, minimum=minimum, maximum=maximum)


def test_configured_idle_range_sleep_fixed_mode_and_busy_time_do_not_change_event_timers():
    assert interval(100) == 120
    assert interval(75) == 150
    assert interval(5) is None
    assert interval(0) is None
    assert interval(75, minimum=180, maximum=900) == 225
    clock = IdleClock(10, adaptive=True, minimum=120, maximum=600)
    assert not clock.state(159, 75)["ready"]
    assert clock.state(160, 75)["ready"]
    assert not clock.state(1000, 75, busy=True)["ready"]
    assert not clock.state(1000, 75, waiting=True)["ready"]
    clock.completed(1000)
    assert clock.state(1000, 75)["remaining_seconds"] == 150
    assert IdleClock(0, fixed=300).state(300, 75)["ready"]
    assert IdleClock(0).state(1000, 75)["interval_seconds"] is None


def test_real_sdk_notifications_reminders_and_admin_messages_bypass_slow_periodic_clock(
    tmp_path, model_server, monkeypatch
):
    script, requests, model = model_server
    script.extend([[("world_finish", {})]] * 3)
    monkeypatch.setattr("astcho_world.ingress.random.uniform", lambda *args: 0.01)
    (tmp_path / "qq.json").write_text(MANIFEST.model_dump_json())
    config = tmp_path / "apps.json"
    config.write_text(json.dumps({"apps": [{"manifest": "qq.json"}]}))
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps({"default": [{"start": "00:00", "end": "23:59", "talk_value": 75}]})
    )
    app = create_app(
        config_path=config,
        data_dir=tmp_path / "world",
        bot_id="10001",
        ingress_token=None,
        model=model,
        schedule_path=schedule,
        adaptive_wake=True,
        wake_min_seconds=120,
        wake_max_seconds=600,
        admins={"10004"},
    )

    async def complete(count):
        async with asyncio.timeout(10):
            while len(requests) < count or not app.state.kernel.store.get(
                "environment_activity", {}
            ).get("finished"):
                await asyncio.sleep(0.01)
        assert app.state.kernel.store.get("idle_wake")["interval_seconds"] == 150

    with TestClient(app) as client:
        assert requests == []  # Starting the host does not force an initial decision.
        with client.websocket_connect("/onebot/v11/ws", headers={"X-Self-ID": "10001"}) as ws:
            ws.send_json(event())
            client.portal.call(complete, 1)
            # Native reminder delivery feeds this same trusted World notification path.
            client.portal.call(
                app.state.kernel.store.notify,
                "world",
                {
                    "id": "synthetic-due-reminder",
                    "source": "自己设置的提醒",
                    "message": "合成的到期提醒",
                    "locator": {},
                },
            )
            client.portal.call(complete, 2)
            private = event(mid=124)
            private.pop("group_id")
            private.update(
                message_type="private",
                user_id=10004,
                message=[{"type": "text", "data": {"text": "合成管理员请求"}}],
            )
            ws.send_json(private)
            client.portal.call(complete, 3)
            assert len(requests) == 3
            assert (
                client.portal.call(
                    lambda: app.state.kernel.store.db.execute(
                        "SELECT count(*) FROM operations WHERE effect='write'"
                    ).fetchone()[0]
                )
                == 0
            )
