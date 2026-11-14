"""W1（Windowsで動くこと）の再検証で足した受け入れ試験（検証担当。修正後の先頭 9a190a7 に対して）。

1回目の受け入れ試験は `test_w1_windows.py`。実測値と測り方は `docs/verification/W1-verifier-2.md` にある。

- 打ち切られた実行は、合格を出力した後でも error になる（読み残した出力が、合格に化けない）。
- Windows でだけ通る分岐（`claude` として起動するファイルの名前、打ち切りと中断で止める範囲、
  開かれているファイルの置き換えの待ち）の動作を壊すと、ハーネスの試験が落ちること。
  Linux では壊しても動作が変わらず確かめられないので、Windows の自動テストでだけ実行する。

期待値の作り方:
- 偽の claude と一時リポジトリは、このファイルの中で作る。
- 壊し方は、ハーネスの試験（tests/test_harness.py）とスクリプトを別の場所に写し、そこで試験を起動するときに、
  スクリプトの関数と定数を差し替えて作る（ソースの文字列は書き換えない）。

時間の許容の根拠:
- 打ち切りの後に戻るまでの猶予（GRACE）は、1回目の受け入れ試験と同じ（Linux 3 秒、Windows 10 秒）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CYCLE = ROOT / "scripts" / "cycle.py"
WINDOWS = os.name == "nt"
GRACE = 10.0 if WINDOWS else 3.0

EMPTY_GIT_CONFIG = Path(tempfile.gettempdir()) / "circuit-sim-w1-recheck-empty.gitconfig"
EMPTY_GIT_CONFIG.write_text("", encoding="utf-8")


def base_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "w1", "GIT_AUTHOR_EMAIL": "w1@example.invalid",
        "GIT_COMMITTER_NAME": "w1", "GIT_COMMITTER_EMAIL": "w1@example.invalid",
        "GIT_CONFIG_GLOBAL": str(EMPTY_GIT_CONFIG), "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.update(extra)
    return env


# ================================================================ 打ち切りと、読み残した出力

PASSING = {
    "type": "result", "subtype": "success", "is_error": False, "session_id": "sess-1",
    "structured_output": {"verdict": "pass", "summary": "ok", "criteria": [], "issues": []},
}

# 合格の結果を出力し終えてから、終わらずに居座る偽の claude。
# 引数に child があれば、出力の口を受け継いだ子プロセスも残す（release のファイルが置かれるまで、最長 40 秒）
PRINTS_A_PASS_AND_STAYS = """\
import pathlib
import subprocess
import sys
import time

here = pathlib.Path(__file__).resolve().parent
if (here / "leave-a-child").exists():
    waiter = (
        "import pathlib, sys, time\\n"
        "end = time.monotonic() + 40\\n"
        "while time.monotonic() < end and not pathlib.Path(sys.argv[1]).exists():\\n"
        "    time.sleep(0.05)\\n"
    )
    command = [sys.executable, "-c", waiter, str(here / "release")]
    subprocess.Popen(command, stdout=sys.stdout, stderr=sys.stderr)
sys.stdout.buffer.write((here / "out.json").read_bytes())
sys.stdout.flush()
(here / "printed").touch()
time.sleep(60)
"""


def git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=base_env(),
    )
    assert proc.returncode == 0, proc.stderr


def started_repo(base: Path) -> Path:
    """start まで済ませた一時リポジトリ。"""
    root = base / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "plan").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text("print('checked')\n", encoding="utf-8")
    (root / "docs" / "plan" / "U1-demo.md").write_text(
        "---\nid: U1\nowner: solver-engineer\n---\n\n# U1: 再検証用の作業単位\n", encoding="utf-8",
    )
    (root / ".gitignore").write_text(".cycle/\n", encoding="utf-8")
    (root / "product.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    proc = subprocess.run(
        [sys.executable, str(CYCLE), "start", "U1"], capture_output=True, text=True, encoding="utf-8",
        env=base_env(CYCLE_ROOT=str(root)), stdin=subprocess.DEVNULL, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return root


def install_staying_claude(base: Path, leave_a_child: bool) -> Path:
    fake = base / "bin"
    fake.mkdir()
    (fake / "out.json").write_text(json.dumps(PASSING), encoding="utf-8")
    if leave_a_child:
        (fake / "leave-a-child").touch()
    if WINDOWS:     # npm で入れた CLI と同じ形（claude.cmd が実体を呼ぶ）
        script = fake / "claude-script.py"
        script.write_text(PRINTS_A_PASS_AND_STAYS, encoding="utf-8")
        (fake / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="oem")
    else:
        command = fake / "claude"
        command.write_text(f"#!{sys.executable}\n{PRINTS_A_PASS_AND_STAYS}", encoding="utf-8")
        command.chmod(0o755)
    return fake


@pytest.mark.parametrize("leave_a_child", [False, True], ids=["子プロセスなし", "子プロセスを残す"])
def test_run_cut_off_after_printing_a_pass_is_recorded_as_an_error(tmp_path, leave_a_child):
    """合格の結果を出力し終えていても、時間内に終わらなかった実行は error（決まり6）。

    打ち切った後は出力の終わりを待たないが、読めた分を結果として使うこともしない。
    """
    root = started_repo(tmp_path)
    fake = install_staying_claude(tmp_path, leave_a_child)
    timeout = 3
    env = base_env(CYCLE_ROOT=str(root), PATH=f"{fake}{os.pathsep}{os.environ['PATH']}")
    proc = subprocess.Popen(
        [sys.executable, str(CYCLE), "run", "U1", "review", "--timeout", str(timeout)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
        stdin=subprocess.DEVNULL,
    )
    began = time.monotonic()
    try:
        try:
            out, err = proc.communicate(timeout=timeout + GRACE)
            returned_in = time.monotonic() - began
        except subprocess.TimeoutExpired:
            returned_in = None
    finally:
        (fake / "release").touch()              # 残った子プロセスを終わらせる
        out, err = proc.communicate(timeout=60)
    assert (fake / "printed").exists()          # 打ち切りの前に、合格の結果は出力し終えていた
    assert returned_in is not None, f"{timeout + GRACE} 秒たっても戻らない（--timeout {timeout}）"
    assert returned_in >= timeout               # 打ち切りの時間までは待った（先に合格として戻っていない）
    assert proc.returncode == 1 and "review: error" in out, out + err
    state = json.loads((root / ".cycle" / "state" / "U1.json").read_text(encoding="utf-8"))
    assert state["gates"]["review"]["verdict"] == "error"
    assert [entry["verdict"] for entry in state["history"] if entry.get("gate") == "review"] == ["error"]


# ================================================================ Windows でだけ通る分岐を壊す

# 写した先でハーネスの試験を起動するときに、スクリプトの動作を差し替える差し込み。
# W1_RECHECK_BREAK の値で、壊し方を選ぶ（none は、何も差し替えない）
BREAK_PLUGIN = """\
import os


def pytest_collection_modifyitems(items):
    script = items[0].module.CYCLE_MODULE
    kind = os.environ["W1_RECHECK_BREAK"]
    if kind == "bare_name":                 # 拡張子の無い同名のファイルも、コマンドの候補にする
        names = script.command_file_names
        script.command_file_names = lambda name: [name, *names(name)]
    elif kind == "no_cmd_by_default":       # PATHEXT が無いときの既定から .CMD を外す
        assert ".CMD" in script.DEFAULT_PATHEXT
        script.DEFAULT_PATHEXT = ".COM;.EXE;.BAT"
    elif kind == "kill_only_the_launcher":  # 止めるのは起動したプロセスだけ（その先は止めない）
        assert callable(script.kill_tree)
        script.kill_tree = lambda proc: proc.kill()
    elif kind == "busy_no_wait":            # 他のプログラムが開いているファイルの置き換えを、待たずに諦める
        assert script.BUSY_WAIT_SECONDS > 0
        script.BUSY_WAIT_SECONDS = 0.0
    elif kind != "none":
        raise ValueError(kind)
"""

# 壊し方 → それで落ちるはずの、ハーネスの試験
BREAKS = {
    "bare_name": "test_file_named_claude_that_cannot_be_launched_is_not_picked",
    "no_cmd_by_default": "test_claude_is_found_when_pathext_is_not_set",
    "kill_only_the_launcher": "test_interrupted_headless_run_stops_the_cli",
    "busy_no_wait": "test_record_is_written_while_another_program_has_the_state_file_open",
}


def run_harness_tests(copy: Path, plugins: Path, kind: str, names: list[str]) -> subprocess.CompletedProcess:
    """写した先で、名前を選んでハーネスの試験を実行する。"""
    env = base_env(PYTHONPATH=str(plugins), W1_RECHECK_BREAK=kind)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_harness.py", "-q", "-p", "no:cacheprovider",
         "-p", "w1_recheck_break", "-k", " or ".join(names), f"--rootdir={copy}",
         f"--basetemp={copy.parent / ('tmp-' + kind)}"],
        cwd=copy, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300,
    )


ONLY_ON_WINDOWS = "Windows でだけ通る分岐を壊す確認（他のOSでは、壊しても動作が変わらない）"


@pytest.mark.skipif(not WINDOWS, reason=ONLY_ON_WINDOWS)
def test_breaking_a_branch_only_windows_takes_makes_a_harness_test_fail(tmp_path):
    """Windows でだけ通る分岐の動作を壊すと、その分岐を確かめるハーネスの試験が落ちる（生き残らない）。"""
    copy, plugins = tmp_path / "copy", tmp_path / "plugins"
    shutil.copytree(ROOT / "scripts", copy / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_harness.py", copy / "tests" / "test_harness.py")
    (copy / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    plugins.mkdir()
    (plugins / "w1_recheck_break.py").write_text(BREAK_PLUGIN, encoding="utf-8")

    # 壊さなければ、選んだ試験はすべて通る（写した先で、試験が実際に実行されている）
    whole = run_harness_tests(copy, plugins, "none", list(BREAKS.values()))
    assert whole.returncode == 0 and f"{len(BREAKS)} passed" in whole.stdout, whole.stdout + whole.stderr

    survived, broken = [], []
    for kind, name in BREAKS.items():
        result = run_harness_tests(copy, plugins, kind, [name])
        if result.returncode == 0:
            survived.append(kind)                               # 壊しても、試験が通ってしまった
        elif result.returncode != 1 or "1 failed" not in result.stdout:
            broken.append((kind, result.returncode, result.stdout[-400:] + result.stderr[-400:]))
    assert not broken, broken                   # 試験の失敗ではない終わり方（差し替えの誤りなど）
    assert survived == []
