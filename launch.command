#!/bin/bash
# Practice Helper — macOS / Linux launcher
cd "$(dirname "$0")" || exit 1
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python app.py
elif command -v python3 >/dev/null 2>&1; then
    exec python3 app.py
else
    echo "Python 3.11+ is required but was not found on your PATH."
    read -r -p "Press Return to close."
    exit 1
fi
