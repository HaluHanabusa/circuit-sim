"""1か所ずつ壊す確認の仕組み（scripts/mutants.py）の試験。

- 壊し方の作り方（機械的に作る種類、表示だけの場所を除くこと）。
- 流し方: 試験が落ちれば「落ちた」、通れば「生き残り」、同値として一覧にあれば数えない。
- 後始末: 試験が残したプロセス（親を失った偽の claude など）を止める（25番）。
- 代表: このリポジトリの決まりに関わる壊し方のいくつかを、近い試験で流し、落ちることを確かめる
  （全件は手で流す。docs/design/harness.md の5章）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_harness import TEST_ENV, sh

ROOT = Path(__file__).resolve().parents[1]
MUTANTS = ROOT / "scripts" / "mutants.py"


def load():
    spec = importlib.util.spec_from_file_location("mutants_script", MUTANTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module             # dataclasses が、読み込んだ場所を引く
    spec.loader.exec_module(module)
    return module


M = load()

SAMPLE = '''\
"""説明。"""
NAMES = ("A", "B")


def judge(x, items):
    """説明の文字列は壊さない。"""
    print(f"x == {x == 1}")
    if x == 1 and items:
        return True
    if not items:
        raise ValueError("空")
    while x:
        x -= 1
        continue
    record(x)
    return x if x else False
'''


def test_mutants_are_made_where_decisions_are_and_not_in_text_for_the_screen(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "cycle.py").write_text(SAMPLE, encoding="utf-8")
    made = {m.id: (m.original, m.replacement) for m in M.generated(tmp_path, ("scripts/cycle.py",))}
    assert made == {
        "cycle:<module>:elem:1": ('"A", ', ""),
        "cycle:<module>:elem:2": (', "B"', ""),
        "cycle:judge:bool:1": ("x == 1 and items", "((x == 1) or (items))"),
        "cycle:judge:cmp:1": ("x == 1", "(x) != (1)"),
        "cycle:judge:num:1": ("1", "2"),
        "cycle:judge:const:1": ("True", "False"),
        "cycle:judge:const:2": ("False", "True"),
        "cycle:judge:not:1": ("not items", "(items)"),
        "cycle:judge:del:1": ('raise ValueError("空")', "pass"),
        "cycle:judge:cond:1": ("x", "not (x)"),
        "cycle:judge:cond:2": ("x", "not (x)"),
        "cycle:judge:del:2": ("continue", "pass"),
        "cycle:judge:del:3": ("record(x)", "pass"),
    }
    for mutant in M.generated(tmp_path, ("scripts/cycle.py",)):
        compile(M.mutated_text(tmp_path, mutant), "x", "exec")         # どれも読める形


def fake_repo(base: Path, equivalent: str = "") -> Path:
    """壊す対象（scripts/cycle.py）と、その試験だけを持つリポジトリ。"""
    root = base / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts" / "cycle.py").write_text(textwrap.dedent('''\
        import subprocess, sys


        def judge(x):
            if x > 0:
                return "positive"
            return "other"


        def spawn(marker):
            """親を失っても残るプロセスを起動する（偽の claude が残る場合の模擬）。"""
            code = f"import pathlib, time; pathlib.Path({marker!r}).write_text('alive'); time.sleep(600)"
            subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        '''), encoding="utf-8")
    for name in ("doctor.py", "local_ci.py"):
        (root / "scripts" / name).write_text("", encoding="utf-8")
    (root / "scripts" / "mutants.toml").write_text(equivalent, encoding="utf-8")
    (root / "tests" / "test_fake.py").write_text(textwrap.dedent('''\
        import importlib.util, os, pathlib, time

        here = pathlib.Path(__file__).parents[1]
        spec = importlib.util.spec_from_file_location("c", here / "scripts" / "cycle.py")
        cycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cycle)


        def test_judge():
            assert cycle.judge(1) == "positive"


        def test_spawn():
            marker = os.environ.get("SPAWN_MARKER")
            if marker:
                cycle.spawn(marker)
                deadline = time.monotonic() + 10
                while not pathlib.Path(marker).exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
        '''), encoding="utf-8")
    sh(root, "git", "init", "-q", "-b", "main")
    sh(root, "git", "add", "-A")
    sh(root, "git", "commit", "-q", "-m", "init")
    return root


def run(root: Path, *args: str, env: dict | None = None) -> tuple[subprocess.CompletedProcess, dict]:
    proc = subprocess.run(
        [sys.executable, str(MUTANTS), "run", "--root", str(root), "--file", "scripts/cycle.py",
         "--jobs", "1", "--no-select", *args],
        capture_output=True, text=True, encoding="utf-8", env={**TEST_ENV, **(env or {})}, timeout=600,
    )
    results = sorted((root / ".cycle" / "mutants").glob("result-*.json"))
    assert results, proc.stdout + proc.stderr
    summary = json.loads(results[-1].read_text(encoding="utf-8"))
    for path in results:
        path.unlink()
    return proc, summary


def test_killed_and_surviving_mutants_are_told_apart(tmp_path):
    root = fake_repo(tmp_path)
    wanted = ("cycle:judge:cmp:1", "cycle:spawn:del:1")
    proc, summary = run(root, "--only", *wanted, "--tests", "tests/test_fake.py")
    statuses = {item["id"]: item["status"] for item in summary["results"]}
    # judge の比べ方を反転すると試験が落ちる。spawn の起動を消しても、印の無い試験は気づかない
    assert statuses == {"cycle:judge:cmp:1": "killed", "cycle:spawn:del:1": "survived"}
    assert proc.returncode == 1 and summary["unexpected"] == ["cycle:spawn:del:1"]
    assert "survived: cycle:spawn:del:1" in proc.stdout
    assert (root / "scripts" / "cycle.py").read_text(encoding="utf-8").count("subprocess.Popen") == 1


def test_surviving_mutant_listed_as_equivalent_with_a_reason_is_not_counted(tmp_path):
    listed = '[[equivalent]]\nid = "cycle:spawn:del:1"\nreason = "試験用"\n'
    root = fake_repo(tmp_path, listed)
    proc, summary = run(root, "--only", "cycle:spawn:del:1", "--tests", "tests/test_fake.py")
    assert proc.returncode == 0 and summary["survived"] == ["cycle:spawn:del:1"]
    assert summary["unexpected"] == []


def test_run_that_gives_no_result_is_not_counted_as_killed(tmp_path):
    root = fake_repo(tmp_path)
    proc, summary = run(root, "--only", "cycle:judge:cmp:1", "--tests", "tests/no_such_test.py")
    assert proc.returncode == 1 and summary["errors"] == ["cycle:judge:cmp:1"] and summary["killed"] == 0


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="残ったプロセスを /proc で探す（Linux）")
def test_processes_left_by_the_tests_are_stopped_after_each_run(tmp_path):
    root = fake_repo(tmp_path)
    marker = tmp_path / "spawned"
    proc, summary = run(
        root, "--only", "cycle:judge:cmp:1", "--tests", "tests/test_fake.py::test_spawn",
        env={"SPAWN_MARKER": str(marker)},
    )
    assert marker.exists()                      # 試験は、親を失っても残るプロセスを起動した
    assert summary["leftovers_stopped"] >= 1
    time.sleep(0.3)
    assert str(marker) not in "\n".join(command_lines())       # 止められて、もう無い


def command_lines() -> list[str]:
    lines = []
    for entry in Path("/proc").iterdir():
        try:
            lines.append((entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace"))
        except OSError:
            continue
    return lines


REPRESENTATIVE = {
    "hand:lock-shared": "tests/test_harness.py::test_lock_is_exclusive_and_waits_even_for_a_shared_holder",
    "cycle:git_paths:del:1": (
        "tests/test_harness.py::test_failure_of_git_listing_files_is_an_error_not_an_empty_list"
    ),
}


@pytest.mark.skipif(os.name == "nt", reason="共有のロックは Linux / macOS にだけある")
@pytest.mark.parametrize("mutant", REPRESENTATIVE)
def test_representative_mutants_of_the_harness_are_killed(mutant):
    """決まりに関わる壊し方の代表（H1 からの生き残り）を、近い試験で流すと落ちる。全件は手で流す。"""
    proc = subprocess.run(
        [sys.executable, str(MUTANTS), "run", "--only", mutant, "--no-select", "--jobs", "1",
         "--tests", REPRESENTATIVE[mutant]],
        capture_output=True, text=True, encoding="utf-8", env=TEST_ENV, timeout=600, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 通り: 落ちた 1、生き残り 0" in proc.stdout
