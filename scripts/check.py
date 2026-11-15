#!/usr/bin/env python3
"""統合の前に必ず通す確認。ruff と pytest を順に実行する。

使い方: python scripts/check.py

Windows、macOS、Linux のどれでも同じに動く（scripts/check.sh は、これを呼ぶだけ）。
どちらかが失敗したら、そこで止めて 0 でない終了コードを返す。標準ライブラリだけで動く。

pytest は、終了コードだけでは合格にしない。試験のファイルの中から os._exit(0) を呼ぶと、試験を流しきらずに
終了コード 0 で終われる。pytest に、試験のセッションの結果を書く差し込み（Report）を付けて起動し、
次のすべてを確かめる: セッションが終わりまで届いた（結果が書かれた）、集めた試験が1件以上あり、外された試験
（deselected）が無く、集めた試験のすべてが終わりまで流れた、失敗とエラーが 0、通過が1件以上。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 実行中の Python から起動する（PATH の上にある、別の環境の ruff や pytest を使わない）。
RUFF = [sys.executable, "-m", "ruff", "check", "."]
# -rs は、スキップした試験と理由を最後に一覧にする（OSに無い機能の試験を、どれだけ飛ばしたかが記録に残る）
PYTEST_ARGS = ["-q", "-rs"]

# pytest を起動する子プロセスの最初の処理: このファイルを読み込み、run_pytest を呼ぶ（引数は、このファイルの
# 場所、結果を書くファイル、pytest の引数）。python -c なので、python -m pytest と同じく、実行した場所が
# 読み込む場所の先頭に入る。
BOOTSTRAP = (
    "import importlib.util, sys; "
    "spec = importlib.util.spec_from_file_location('circuit_sim_check', sys.argv[1]); "
    "check = importlib.util.module_from_spec(spec); spec.loader.exec_module(check); "
    "sys.exit(check.run_pytest(sys.argv[2], sys.argv[3:]))"
)


class Report:
    """pytest の差し込み。試験のセッションの結果（件数）を、セッションの終わりにファイルに書く。

    途中で終わったプロセス（os._exit など）は、何も書かない。
    """

    def __init__(self, path: Path):
        self.path = path
        self.collected: int | None = None
        self.deselected = 0
        self.finished: set[str] = set()
        self.counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}

    def pytest_deselected(self, items: list) -> None:
        self.deselected += len(items)

    def pytest_collection_finish(self, session) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "call":
            self.counts[report.outcome] += 1
        elif report.failed:                 # 準備（setup）か後始末（teardown）の失敗
            self.counts["errors"] += 1
        elif report.skipped:                # 準備の段階で飛ばした（skipif など）
            self.counts["skipped"] += 1
        if report.when == "teardown":       # どの試験も、最後に後始末の報告がある
            self.finished.add(report.nodeid)

    def pytest_sessionfinish(self, session) -> None:
        data = {
            "collected": self.collected, "deselected": self.deselected, "finished": len(self.finished),
            **self.counts,
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")


def run_pytest(report: str, args: list[str]) -> int:
    """pytest を、このプロセスの中で、Report を付けて実行する（pytest_command の子プロセスが呼ぶ）。"""
    import pytest  # 確認の子プロセスでだけ読む

    return int(pytest.main(args, plugins=[Report(Path(report))]))


def pytest_command(report: Path) -> list[str]:
    return [sys.executable, "-c", BOOTSTRAP, str(Path(__file__).resolve()), str(report), *PYTEST_ARGS]


def report_problem(report: Path) -> str:
    """pytest が書いた結果が、合格と矛盾すれば、その理由を返す。矛盾しなければ空文字。"""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "試験のセッションが終わりまで届かなかった（結果が書かれていない）"
    collected = data.get("collected")
    if type(collected) is not int or collected < 1:
        return "集めた試験が無い"
    if data.get("deselected"):
        return f"外された試験がある（{data['deselected']} 件。-k、-m などで選び直さない）"
    if data.get("finished") != collected:
        return f"集めた試験 {collected} 件のうち、終わりまで流れたのは {data.get('finished')} 件"
    if data.get("failed") or data.get("errors"):
        return "失敗かエラーがある"
    if not data.get("passed"):
        return "通過した試験が無い"
    return ""


def main() -> int:
    code = subprocess.run(RUFF, cwd=ROOT, check=False).returncode
    if code != 0:
        return code
    with tempfile.TemporaryDirectory(prefix="circuit-sim-check-") as tmp:
        report = Path(tmp) / "report.json"
        code = subprocess.run(pytest_command(report), cwd=ROOT, check=False).returncode
        if code != 0:
            return code
        problem = report_problem(report)
    if problem:
        # 標準エラーは、出せない文字を置き換えて書くので、UTF-8 でない端末でも落ちない（Python の決まり）
        print(f"確認: pytest は終了コード 0 で終わったが、{problem}。合格にしない", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
