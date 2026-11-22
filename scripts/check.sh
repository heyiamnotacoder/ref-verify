#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if command -v ruff >/dev/null 2>&1; then
  RUFF=(ruff)
elif command -v uvx >/dev/null 2>&1; then
  RUFF=(uvx ruff)
else
  echo "ERROR: ruff (or uvx) is required to run lint/format checks." >&2
  exit 1
fi

"${RUFF[@]}" check src tests
"${RUFF[@]}" format --check src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
