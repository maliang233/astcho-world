"""Environment-only instructions and truthful runtime context; never wraps chat prompts."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from astcho_world.experience import operation_states
from astcho_world.speech import outbound_speech_guidance

SECTIONS = (
    "identity",
    "environment",
    "observations",
    "actions",
    "administration",
    "continuity",
    "social_actions",
)
PROMPT_DIRECTORY = Path(__file__).with_name("prompts")


def environment_prompt(profile_path: Path | None = None) -> str:
    sections = [(PROMPT_DIRECTORY / f"{name}.md").read_text() for name in SECTIONS]
    sections.append(outbound_speech_guidance())
    if profile_path is not None:
        # Keep the configured persona text byte-for-byte after UTF-8 decoding.
        # Do not copy or rewrite any protected ChatService prompt template.
        original = profile_path.read_bytes().decode("utf-8")
        try:
            profile = json.loads(original)
        except ValueError:
            raise ValueError("Environment identity must be a valid JSON object") from None
        if not isinstance(profile, dict):
            raise ValueError("Environment identity must be a JSON object")
        sections.append("## 已配置的身份资料（原文）\n\n" + original)
    return "\n\n".join(sections)


def runtime_context(kernel, *, step, max_steps, previous_activity, wake_policy, provider=None):
    now = datetime.now().astimezone()
    context = {
        "type": "world.runtime",
        "source": "world",
        "clock": {
            "unix_seconds": time.time(),
            "local_time": now.isoformat(timespec="seconds"),
            "timezone": str(now.tzinfo),
        },
        "activity": {
            "model_call": step + 1,
            "max_model_calls": max_steps,
            "remaining_including_current": max_steps - step,
            "previous": previous_activity,
            "history": "recent_turns_only; not a complete personal memory",
        },
        "wake_policy": wake_policy,
        "uncertain_operations": operation_states(kernel.store),
        "apps": kernel.catalog(),
        "ambient": {
            app_id: {**app.ambient, "stale": time.time() - app.ambient["updated"] > 60}
            for app_id, app in kernel.apps.items()
            if app.ambient
        },
    }
    if provider is not None:
        context["self_state"] = provider()
    return {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
