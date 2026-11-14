#!/usr/bin/env bash
# 統合前に必ず通す確認。中身は scripts/check.py（どのOSでも同じに動く）。使い方: scripts/check.sh
set -euo pipefail
exec python "$(dirname "$0")/check.py"
