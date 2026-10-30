from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment(path: Path) -> None:
    """Load only the selected local env file; shell values retain precedence."""
    if path.is_file():
        load_dotenv(path, override=False)


def require_environment(*names: str) -> dict[str, str]:
    values = {name: os.getenv(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))
    return values


def apply_paths(args) -> None:
    if args.data_dir is None:
        args.data_dir = Path(os.getenv("ASTCHO_WORLD_DATA_DIR", "var")).expanduser()
    if args.skills is None and os.getenv("ASTCHO_WORLD_SKILLS_PATH"):
        args.skills = Path(os.environ["ASTCHO_WORLD_SKILLS_PATH"]).expanduser()
    if hasattr(args, "schedule") and args.schedule is None and os.getenv("ASTCHO_SCHEDULE_PATH"):
        args.schedule = Path(os.environ["ASTCHO_SCHEDULE_PATH"]).expanduser()
