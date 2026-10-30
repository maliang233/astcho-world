#!/bin/zsh
cd -- "${0:A:h}"
exec /usr/bin/arch -arm64 .venv/bin/python tools/launch_mac.py --stop
