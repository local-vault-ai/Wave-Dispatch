#!/bin/sh
# Wave Dispatch one-command setup (macOS / Linux). Windows: see the README (py -m venv .venv ...).
# Finds Python 3.10+, makes a private .venv in this folder (pyyaml goes there, nothing system-wide), then runs the
# setup helper. Run it again any time: it reuses the .venv.
cd "$(dirname "$0")" || exit 1

new_enough() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

PY=""
for p in .venv/bin/python python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3 python; do
    if command -v "$p" >/dev/null 2>&1 && new_enough "$p"; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
    have=$(python3 --version 2>&1 || echo "no Python")
    echo "Wave Dispatch needs Python 3.10 or newer; this computer has: $have"
    case "$(uname -s)" in
        Darwin) echo "Install it from https://www.python.org/downloads/ (or, with Homebrew: brew install python)," ;;
        *)      echo "Install it with your package manager (Ubuntu/Debian: sudo apt install python3 python3-venv)," ;;
    esac
    echo "then run ./setup.sh again."
    exit 1
fi

if [ "$PY" != ".venv/bin/python" ]; then
    echo "Using $("$PY" --version 2>&1) to make a private .venv in this folder ..."
    "$PY" -m venv .venv || { echo "Could not create .venv (Ubuntu/Debian: sudo apt install python3-venv)"; exit 1; }
fi
.venv/bin/python -c "import yaml" 2>/dev/null || .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1
exec .venv/bin/python wave_dispatch_setup.py "$@"
