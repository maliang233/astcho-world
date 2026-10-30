from __future__ import annotations

import asyncio
import importlib
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path


async def serve():
    module, factory = sys.argv[1].split(":")
    # stdout belongs exclusively to the wire protocol, including during imports.
    with redirect_stdout(sys.stderr):
        app = getattr(importlib.import_module(module), factory)(
            Path(sys.argv[2]), json.loads(sys.argv[3])
        )
    while line := await asyncio.to_thread(sys.stdin.readline):
        request = json.loads(line)
        from astcho_world.usage_capture import operation_id

        telemetry_token = operation_id.set(request["params"].get("operation_id"))
        try:
            with redirect_stdout(sys.stderr):
                result = await app.dispatch(request["method"], request["params"])
            response = {"id": request["id"], "result": result}
            if hasattr(app, "ambient_state"):
                try:
                    with redirect_stdout(sys.stderr):
                        projection = app.ambient_state()
                        json.dumps(projection, allow_nan=False)
                        response["ambient_state"] = projection
                except Exception:
                    traceback.print_exc(file=sys.stderr)
            if hasattr(app, "dashboard_state"):
                try:
                    with redirect_stdout(sys.stderr):
                        projection = app.dashboard_state()
                        # Optional telemetry must never break a settled business reply.
                        json.dumps(projection, allow_nan=False)
                        response["dashboard_state"] = projection
                except Exception:
                    traceback.print_exc(file=sys.stderr)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = {"id": request["id"], "error": f"{type(exc).__name__}: {exc}"}
        finally:
            operation_id.reset(telemetry_token)
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(serve())
