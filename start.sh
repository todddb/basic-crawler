#!/bin/bash

if [ "$EUID" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo "$0" "$@"
    else
        echo "This service must be run as root to manage Wi-Fi and hardware." >&2
        exit 1
    fi
fi

source venv/bin/activate
python backend/app.py
