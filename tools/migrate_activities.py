"""Offline migration; run only against a private copy first. Never sends App actions."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from astcho_world.cli import environment_model
from astcho_world.dsh_runtime import DSHAgent
from astcho_world.kernel import Kernel


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Original DSH workspace path, preserving the Session lookup key",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--refresh-summaries",
        action="store_true",
        help="Compatibility name: reuse valid indexes and build missing local facts; no LLM",
    )
    args = parser.parse_args()
    load_dotenv(args.env_file)
    kernel = Kernel(args.data_dir)
    agent = None
    try:
        count = kernel.store.db.execute(
            "SELECT count(DISTINCT e.activity_id) FROM events e LEFT JOIN activity_runs r "
            "ON e.activity_id=r.run_id WHERE e.activity_id IS NOT NULL AND r.run_id IS NULL"
        ).fetchone()[0]
        if not args.apply:
            print(
                json.dumps(
                    {
                        "unindexed_legacy_runs": count,
                        "mode": "preview",
                        "finished_activities": len(kernel.activities.finished_ids()),
                    }
                )
            )
            return
        agent = DSHAgent(
            kernel,
            environment_model("dsh"),
            activity_context=True,
            workspace_path=args.workspace,
            profile_path=Path(os.environ["ASTCHO_PROFILE_PATH"])
            if os.getenv("ASTCHO_PROFILE_PATH")
            else None,
        )
        print(
            json.dumps(
                await agent.refresh_activity_summaries()
                if args.refresh_summaries
                else await agent.migrate_legacy()
            )
        )
        print(
            json.dumps(
                {
                    "archives": len(kernel.activities.legacy_ids()),
                    "recent_briefs": len(kernel.activities.recent(10)),
                }
            )
        )
    finally:
        if agent:
            await agent.close()
        await kernel.close()


if __name__ == "__main__":
    asyncio.run(main())
