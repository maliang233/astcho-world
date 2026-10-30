"""Install a small Finder application pointing at this checkout (no bundled runtime)."""

import platform
import plistlib
import shlex
from pathlib import Path

root = Path(__file__).resolve().parents[1]
app = Path.home() / "Applications" / "Astcho World.app"
macos = app / "Contents" / "MacOS"
macos.mkdir(parents=True, exist_ok=True)
info = {
    "CFBundleName": "Astcho World",
    "CFBundleDisplayName": "星回 Astcho World",
    "CFBundleIdentifier": "local.astcho.world.launcher",
    "CFBundleVersion": "1.0",
    "CFBundlePackageType": "APPL",
    "CFBundleExecutable": "astcho-world",
    "LSUIElement": True,
    "LSArchitecturePriority": [platform.machine()],
}
(app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
script = macos / "astcho-world"
script.write_text(
    "#!/bin/zsh\nexec /usr/bin/arch -"
    + platform.machine()
    + " "
    + shlex.quote(str(root / ".venv/bin/python"))
    + " "
    + shlex.quote(str(root / "tools/launch_mac.py"))
    + "\n"
)
script.chmod(0o755)
print(app)
