"""Composition of shipped DSH plugins; no replacement search/skill/scheduler engine."""

import os
from pathlib import Path


def plugin_rows(root: Path, skills: Path | None):
    def plugin(name, config=None):
        row = {"id": name, "name": f"@deepseek-ai/dsh-{name}"}
        if config is not None:
            row["config"] = config
        return row

    return [
        plugin("web", {"searchProvider": "deepseek-official", "fetchProvider": "http"}),
        plugin("web-fetch-http"),
        plugin("web-search-deepseek", {"apiKeyEnv": "DEEPSEEK_API_KEY"}),
        plugin("tool-web"),
        plugin("tool-call-timeout-policy"),
        plugin("skill"),
        plugin(
            "skill-filesystem",
            {
                "includeDefaultRoots": False,
                "customSkillDirs": [str(skills.resolve())] if skills else [],
            },
        ),
        plugin("tool-skill"),
        plugin("storage"),
        plugin("storage-json", {"root": str(root / "storages")}),
        plugin("storage-domain", {"backend": "json"}),
        {
            "id": "world-schedule-host",
            "name": str(Path(__file__).with_name("dsh") / "schedule.mjs"),
        },
        plugin("schedule"),
        plugin("tool-schedule"),
    ]


def capability_status():
    return {
        "web_search": {
            "provider": "deepseek-official",
            "configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "credential_env": "DEEPSEEK_API_KEY",
        },
        "web_fetch": {"provider": "http", "configured": True},
        "reminders": {"backend": "dsh-schedule"},
        "skills": {"backend": "dsh-skill-filesystem"},
    }
