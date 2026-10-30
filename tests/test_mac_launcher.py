import importlib.util
from pathlib import Path


def launcher():
    path = Path(__file__).resolve().parents[1] / "tools/launch_mac.py"
    spec = importlib.util.spec_from_file_location("astcho_test_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_reuses_service_without_starting_second_instance(tmp_path, monkeypatch):
    launch = launcher()
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").touch()
    monkeypatch.setattr(launch, "healthy", lambda token: True)
    monkeypatch.setattr(launch, "service_mode", lambda token: "live")
    opened = []
    monkeypatch.setattr(launch.webbrowser, "open", opened.append)
    monkeypatch.setattr(launch.sys, "argv", ["launcher"])
    monkeypatch.setattr(
        launch.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("duplicate process")),
    )
    monkeypatch.chdir(tmp_path)
    assert launch.main() == 0
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:8091/#token=")
    assert (tmp_path / "var/launcher/web-token").stat().st_mode & 0o777 == 0o600


def test_launcher_missing_configuration_fails_without_observer_fallback(tmp_path, monkeypatch):
    launch = launcher()
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").touch()
    monkeypatch.setattr(launch.sys, "argv", ["launcher"])
    for key in (
        "ASTCHO_WORLD_BOT_ID",
        "ASTCHO_WORLD_MODEL",
        "ASTCHO_WORLD_API_KEY",
        "ASTCHO_WORLD_BASE_URL",
        "ASTCHO_WORLD_INGRESS_TOKEN",
        "ASTCHO_ADMINS",
    ):
        monkeypatch.delenv(key, raising=False)
    checks = iter([False, True])
    monkeypatch.setattr(launch, "healthy", lambda token: next(checks))
    messages, calls = [], []
    monkeypatch.setattr(launch, "alert", messages.append)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: None)

    class Process:
        pid = 123

        def poll(self):
            return None

    def spawn(args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    monkeypatch.chdir(tmp_path)
    assert launch.main() == 1
    assert calls == []
    assert "ASTCHO_ADMINS" in messages[0]


def test_launcher_complete_configuration_starts_subject_and_web(tmp_path, monkeypatch):
    launch = launcher()
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").touch()
    monkeypatch.setattr(launch.sys, "argv", ["launcher"])
    for key in (
        "ASTCHO_WORLD_BOT_ID",
        "ASTCHO_WORLD_MODEL",
        "ASTCHO_WORLD_API_KEY",
        "ASTCHO_WORLD_BASE_URL",
        "ASTCHO_WORLD_INGRESS_TOKEN",
        "ASTCHO_ADMINS",
    ):
        monkeypatch.setenv(key, "synthetic")
    checks = iter([False, True])
    monkeypatch.setattr(launch, "healthy", lambda token: next(checks))
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: None)
    calls = []

    class Process:
        pid = 123

        def poll(self):
            return None

    def spawn(args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    monkeypatch.chdir(tmp_path)
    assert launch.main() == 0
    assert calls[0][5] == "serve"
    assert calls[0][6:] == ["--qq-transport", "reverse_ws", "--runner", "dsh"]


def test_launcher_replaces_its_existing_observer_with_live_service(tmp_path, monkeypatch):
    launch = launcher()
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/python").touch()
    (tmp_path / "var/launcher").mkdir(parents=True)
    (tmp_path / "var/launcher/server.pid").write_text("123")
    monkeypatch.setattr(launch.sys, "argv", ["launcher"])
    for key in (
        "ASTCHO_WORLD_MODEL",
        "ASTCHO_WORLD_API_KEY",
        "ASTCHO_WORLD_BASE_URL",
        "ASTCHO_ADMINS",
    ):
        monkeypatch.setenv(key, "synthetic")
    checks = iter([True, True])
    monkeypatch.setattr(launch, "healthy", lambda token: next(checks))
    monkeypatch.setattr(launch, "service_mode", lambda token: "observer")
    stopped = []
    monkeypatch.setattr(launch, "stop_managed", lambda path: stopped.append(path) or True)
    monkeypatch.setattr(launch.webbrowser, "open", lambda url: None)
    calls = []

    class Process:
        pid = 456

        def poll(self):
            return None

    monkeypatch.setattr(
        launch.subprocess, "Popen", lambda args, **kwargs: calls.append(args) or Process()
    )
    assert launch.main() == 0
    assert len(stopped) == 1
    assert calls[0][5] == "serve"
