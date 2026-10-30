from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from jsonschema import validate

from astcho_world.contracts import Manifest


class AppProcess:
    def __init__(self, manifest: Manifest, data_dir: Path, config: dict | None = None):
        self.manifest, self.data_dir = manifest, data_dir
        self.config = config or {}
        self.process = None
        self.lock = asyncio.Lock()
        self.log = None
        self.enabled = True
        self.ambient = None

    async def start(self):
        if not self.enabled:
            raise RuntimeError("Application is stopped")
        if self.process is not None and self.process.returncode is None:
            return
        if self.log is not None:
            self.log.close()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = (self.data_dir / "worker.log").open("ab")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        self.process = await asyncio.create_subprocess_exec(
            self.config.get("runtime_python", sys.executable),
            "-m",
            "astcho_world.worker",
            self.manifest.entrypoint,
            str(self.data_dir),
            json.dumps(self.config),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.log,
            env=env,
            limit=4 * 1024 * 1024,
        )

    async def request(self, method: str, params: dict, timeout: float):
        # Serialization and timeout are per application, not a global lock.
        async with self.lock:
            await self.start()
            request_id = uuid.uuid4().hex
            payload = json.dumps({"id": request_id, "method": method, "params": params})
            try:
                async with asyncio.timeout(timeout):
                    self.process.stdin.write(payload.encode() + b"\n")
                    await self.process.stdin.drain()
                    line = await self.process.stdout.readline()
                    if not line:
                        raise ConnectionError("Application exited")
                    result = json.loads(line)
                    if result.get("id") != request_id:
                        raise ConnectionError("Mismatched response ID")
                    if result.get("error"):
                        raise ValueError(result["error"])
                    if "ambient_state" in result and self.manifest.ambient_schema:
                        try:
                            validate(result["ambient_state"], self.manifest.ambient_schema)
                            self.ambient = {"updated": time.time(), "data": result["ambient_state"]}
                        except Exception:
                            pass  # Optional projection must never change write settlement.
                    if "dashboard_state" in result:
                        try:
                            state = {"updated": time.time(), "data": result["dashboard_state"]}
                            temporary = self.data_dir / "dashboard-state.tmp"
                            temporary.write_text(json.dumps(state, ensure_ascii=False))
                            temporary.replace(self.data_dir / "dashboard-state.json")
                        except OSError:
                            # An administrative projection must not change action settlement.
                            pass
                    return result["result"]
            except BaseException:
                await self.stop()
                raise

    async def stop(self):
        process, self.process = self.process, None
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        if self.log is not None:
            self.log.close()
            self.log = None
