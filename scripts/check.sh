#!/usr/bin/env bash
# マージ前に必ず通す確認。使い方: scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check .
python -m pytest -q
