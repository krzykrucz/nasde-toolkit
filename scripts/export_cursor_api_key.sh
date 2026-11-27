#!/bin/bash

# Export a Cursor API key as CURSOR_API_KEY for nasde's Cursor agent.
#
# Harbor's Cursor agent authenticates only via CURSOR_API_KEY. Source this
# script before running nasde:
#
#   source scripts/export_cursor_api_key.sh
#   nasde run --variant cursor-vanilla -C my-benchmark
#
# Resolution order:
#   1. An already-exported CURSOR_API_KEY is kept.
#   2. macOS: Keychain entry "cursor-api-key" (stored by the cursor-agent CLI).
# Otherwise create a key in the Cursor dashboard (Integrations → API keys) and
# export it yourself, or put CURSOR_API_KEY=<key> in .env in the benchmark
# project dir (or its parent) — `nasde run` reads that file itself, so this
# script is then not needed. Windows users: see scripts/export_cursor_api_key.ps1.

# NOTE: Do NOT use `set -e` here -- this script is sourced into the user's
# shell, so errexit would persist and kill the terminal on any later non-zero
# exit code.

if [ -n "$CURSOR_API_KEY" ]; then
    echo "✓ CURSOR_API_KEY already set (${CURSOR_API_KEY:0:8}...)"
    return 0 2>/dev/null || exit 0
fi

_cursor_key=""
if command -v security >/dev/null 2>&1; then
    _cursor_key="$(security find-generic-password -s "cursor-api-key" -w 2>/dev/null)"
fi

if [ -z "$_cursor_key" ]; then
    echo "ERROR: Could not find a Cursor API key." >&2
    echo "  - macOS: keychain entry 'cursor-api-key' missing" >&2
    echo "Create one in the Cursor dashboard (Integrations → API keys), then either:" >&2
    echo "  export CURSOR_API_KEY=<your-key>" >&2
    echo "  or add CURSOR_API_KEY=<your-key> to .env in the benchmark project dir (or its parent)" >&2
    unset _cursor_key
    return 1 2>/dev/null || exit 1
fi

export CURSOR_API_KEY="$_cursor_key"
unset _cursor_key

echo "✓ CURSOR_API_KEY exported (${CURSOR_API_KEY:0:8}...)"
