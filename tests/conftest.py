"""試験の全体に掛かる設定。

試験の中から、このリポジトリの試験がもう一度実行されること（入れ子）を止める。
確認スクリプト（scripts/check.py）や環境の確認（scripts/doctor.py）を起動する試験で、起動する場所の指定が
壊れると、本物の scripts/check.py → pytest → 同じ試験 → … と続いて、終わらなくなるため。
試験を集めるだけの起動（--collect-only）は、実行しないので止めない。
"""
import os

import pytest

RUNNING = "CIRCUIT_SIM_PYTEST_RUNNING"
NESTED_EXIT_CODE = 70


def pytest_sessionstart(session: pytest.Session) -> None:
    config = session.config
    if hasattr(config, "workerinput"):      # 並列実行の子（pytest-xdist）は、同じ1回の実行の一部
        return
    if os.environ.get(RUNNING) and not config.option.collectonly:
        pytest.exit(
            "試験の中から、このリポジトリの試験がもう一度実行された（入れ子）。終わらなくなるので止める",
            returncode=NESTED_EXIT_CODE,
        )
    os.environ[RUNNING] = "1"               # ここから起動するプロセスに引き継がれる
