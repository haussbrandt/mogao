#!/bin/sh

set -eu

if ! command -v uv >/dev/null 2>&1; then
    printf 'Missing required dependency: uv\n'
    printf 'Install it from https://docs.astral.sh/uv/getting-started/installation/\n'
    printf 'Then run ./quickstart.sh again.\n'
    exit 1
fi

exec uv run --locked python scripts/quickstart.py
