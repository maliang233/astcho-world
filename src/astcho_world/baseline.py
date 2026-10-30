from __future__ import annotations

import hashlib
import json
from pathlib import Path


def verify(root: Path, *, original: Path | None = None) -> list[str]:
    manifest = json.loads((root / "docs" / "legacy-baseline.json").read_text())
    errors = []
    for rel, expected in manifest["files"].items():
        copied = (
            root / rel if rel.startswith("src/") else root / "tests" / "legacy" / Path(rel).name
        )
        for path in [copied, original / rel] if original else [copied]:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                errors.append(str(path))
    return errors
