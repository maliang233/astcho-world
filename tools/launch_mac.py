#!/usr/bin/env python3
"""Finder launcher: one local instance, private logs, readiness check, browser handoff."""

from __future__ import annotations

import fcntl
import http.cookiejar
import json
import os
import platform
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8091
URL = f"http://127.0.0.1:{PORT}"


def alert(message):
    # Message enters AppleScript through argv, never interpolated as executable text.
    subprocess.run(
        [
            "osascript",
            "-e",
            'on run argv\ndisplay dialog (item 1 of argv) with title "Astcho World" '
            'buttons {"好"} default button "好"\nend run',
            message,
        ],
        check=False,
    )


def healthy(token):
    try:
        data = json.dumps({"token": token}).encode()
        request = urllib.request.Request(
            URL + "/api/session", data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def service_mode(token):
    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        request = urllib.request.Request(
            URL + "/api/session",
            data=json.dumps({"token": token}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request, timeout=2):
            pass
        with opener.open(URL + "/api/snapshot", timeout=2) as response:
            return json.load(response).get("mode")
    except (OSError, ValueError):
        return None


def stop_managed(runtime):
    pid_file = runtime / "server.pid"
    if not pid_file.exists():
        return True
    pid = int(pid_file.read_text())
    command = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    ).stdout
    if not command.strip():
        return True
    cwd = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True
    ).stdout.splitlines()
    if "-m astcho_world" not in command or "n" + str(ROOT) not in cwd:
        alert("记录的进程不属于本项目，未停止它。请检查服务端口与启动记录。")
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        status = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
        ).stdout.strip()
        if not status or status.startswith("Z"):
            pid_file.unlink(missing_ok=True)
            return True
        time.sleep(0.2)
    alert("服务还在退出，请稍后再启动。未强行中断操作。")
    return False


def main():
    os.umask(0o077)
    os.chdir(ROOT)
    runtime = ROOT / "var" / "launcher"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        python = ROOT / ".venv/bin/python"
        if not python.is_file():
            alert("尚未安装项目依赖。请先在项目目录创建 .venv，并安装 .[host,agent]。")
            return 1
        from dotenv import dotenv_values

        env = {
            **os.environ,
            **{k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None},
        }
        token_file = runtime / "web-token"
        if token_file.exists():
            token = token_file.read_text().strip()
        else:
            token = secrets.token_urlsafe(32)
            token_file.write_text(token)
        token_file.chmod(0o600)
        link = URL + "/#token=" + token
        if "--stop" in sys.argv or "--restart" in sys.argv:
            if not stop_managed(runtime):
                return 1
            if "--stop" in sys.argv:
                return 0
        if healthy(token):
            if service_mode(token) == "live":
                webbrowser.open(link)
                return 0
            if not (runtime / "server.pid").exists():
                alert("端口已有其他服务，无法切换本项目到正式运行。")
                return 1
            if not stop_managed(runtime):
                return 1
        required = (
            "ASTCHO_WORLD_MODEL",
            "ASTCHO_WORLD_API_KEY",
            "ASTCHO_WORLD_BASE_URL",
            "ASTCHO_ADMINS",
        )
        missing = [key for key in required if not env.get(key, "").strip()]
        if missing:
            alert("星回未启动；请补全 .env 配置：" + "、".join(missing))
            return 1
        args = [
            "/usr/bin/arch",
            "-" + platform.machine(),
            str(python),
            "-m",
            "astcho_world",
            "serve",
            "--qq-transport",
            "reverse_ws",
            "--runner",
            "dsh",
        ]
        env["ASTCHO_WORLD_WEB_TOKEN"] = token
        env["PYTHONUNBUFFERED"] = "1"
        log_path = runtime / "server.log"
        with log_path.open("a") as log:
            process = subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
            )
        (runtime / "server.pid").write_text(str(process.pid))
        for _ in range(60):
            if process.poll() is not None:
                alert(f"启动失败，可能已有服务占用端口或数据目录。\n请查看日志：{log_path}")
                return 1
            if healthy(token):
                webbrowser.open(link)
                return 0
            time.sleep(0.5)
        alert(f"服务启动超时。请查看日志：{log_path}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        alert(f"启动器遇到问题：{type(exc).__name__}: {exc}")
        sys.exit(1)
