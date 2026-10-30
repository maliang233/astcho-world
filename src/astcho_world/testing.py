"""Synthetic offline fault fixtures, never registered by default."""

import asyncio
import os


class FaultApp:
    def __init__(self, data_dir, config):
        pass

    async def dispatch(self, method, params):
        if params["tool"] == "hang":
            await asyncio.sleep(60)
        os._exit(19)
