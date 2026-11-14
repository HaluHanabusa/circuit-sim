#!/usr/bin/env python3
"""統合の前に必ず通す確認。ruff と pytest を順に実行する。

使い方: python scripts/check.py

Windows、macOS、Linux のどれでも同じに動く（scripts/check.sh は、これを呼ぶだけ）。
どちらかが失敗したら、そこで止めて 0 でない終了コードを返す。標準ライブラリだけで動く。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 実行中の Python から起動する（PATH の上にある、別の環境の ruff や pytest を使わない）。
# -rs は、スキップした試験と理由を最後に一覧にする（OSに無い機能の試験を、どれだけ飛ばしたかが記録に残る）
STEPS = (
    [sys.executable, "-m", "ruff", "check", "."],
    [sys.executable, "-m", "pytest", "-q", "-rs"],
)


def main() -> int:
    for step in STEPS:
        code = subprocess.run(step, cwd=ROOT).returncode
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
