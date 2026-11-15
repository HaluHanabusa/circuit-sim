"""H2 範囲25（1か所ずつ壊す確認の後始末）の受け入れ試験（検証担当。ツリー 53e9779f）。

実測値と測り方は `docs/verification/H2-verifier.md` にある。

確かめること: 後始末が止めるのは、その回の印（CIRCUIT_SIM_MUTANT_RUN=<その回の値>）をそのまま持つプロセス
だけで、印の無いプロセス、別の回の印、印に似た文字列を別の変数に持つプロセスは止めない（他人のプロセスを
止めない）。

期待値の作り方: 環境変数を変えた sleep を、この試験の中で起動する。止まったかどうかは、終了を待って
（Popen.poll）見る。scripts/mutants.py の stop_leftovers だけを呼ぶ（探し方の実装は写さない）。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not Path("/proc").is_dir(), reason="後始末は /proc を読む（Linux だけ）")

MARK = "CIRCUIT_SIM_MUTANT_RUN"


def load_mutants():
    spec = importlib.util.spec_from_file_location("h2_acceptance_mutants", ROOT / "scripts" / "mutants.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module         # dataclass が自分のモジュールを引けるように
    spec.loader.exec_module(module)
    return module


def sleeper(extra: dict[str, str]) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k != MARK}
    env.update(extra)
    return subprocess.Popen(["sleep", "120"], env=env, start_new_session=True)


def test_cleanup_stops_only_processes_with_this_runs_mark():
    mutants = load_mutants()
    token = uuid.uuid4().hex
    others = {
        "no_mark": sleeper({}),
        "other_run": sleeper({MARK: uuid.uuid4().hex}),
        "longer_value": sleeper({MARK: token + "0"}),
        "empty_mark": sleeper({MARK: ""}),
        "in_another_variable": sleeper({"H2_DECOY": f"{MARK}={token}"}),
    }
    marked = [sleeper({MARK: token}), sleeper({MARK: token})]
    try:
        time.sleep(0.3)         # 起動して、環境変数が /proc に見えるまで
        stopped = mutants.stop_leftovers(token)
        for proc in marked:
            proc.wait(timeout=10)
        assert stopped == len(marked)
        assert all(proc.returncode is not None for proc in marked)
        assert {name: proc.poll() for name, proc in others.items()} == dict.fromkeys(others)
    finally:
        for proc in [*others.values(), *marked]:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
