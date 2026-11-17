"""A0 の独立検証（verifier、6回目）その3: アプリの試験（tests/test_app_*.py）が、起動したプロセスを残さない。

記録は docs/verification/A0-verifier-6.md。きっかけは 5回目の問題2: PO の PC（Windows 11）で、実装者の試験が
起動したアプリ本体（python -m circuit_sim.app --no-browser --port 50000）が、試験の後も待ち受け続けていた。
実装者は、tests/app_procs.py（起動を控え、試験の終わりに動いていたら止めて失敗にする）で塞いだ。
利用者の PC に待ち受けるプロセスを残さないことは、製品の試験として守る条件なので、ここで確かめる。

1. 実装者の決まり（起動は部品を通す）が、どの試験ファイルでも守られている（構文木で見る）。
2. わざと残す試験を、実装者の部品と見張りで流す（別の pytest）。残したプロセスは止まり、残した試験は
   失敗になる: 「起動に失敗して終わるはず」が起動に成功した（PO の PC で起きた形）、途中で例外で抜けた、
   出力を待つ時間が切れた。Windows でも流れる（taskkill /T の道が、ここで初めて流れる）。
3. 軽微（xfail）: ハーネス（scripts/cycle.py の kill_tree。scripts/local_ci.py が、時間切れと中断で使う）が
   確認をまとまりごと止めたとき、実装者の部品で起動したアプリ本体が残る（POSIX）。部品が、起動するものを
   新しいまとまり（start_new_session）に入れるため。22d2fdc の起動（そのままの Popen）では残らない。

受け入れ試験は、実装者の試験の部品を「確かめる対象」として使う（頼って何かを確かめるのではない）。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import acceptance_watch as watch
import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
WINDOWS = os.name == "nt"

# プロセスを起動する呼び出しのうち、アプリの試験が直接は使わないもの（起動は tests/app_procs.py の spawn か
# Launched を通す。そうすれば控えに入り、試験の終わりに止められる）
DIRECT_STARTERS = {
    ("subprocess", "Popen"), ("os", "system"), ("os", "popen"), ("os", "startfile"),
    ("os", "spawnl"), ("os", "spawnle"), ("os", "spawnlp"), ("os", "spawnv"), ("os", "spawnve"),
    ("os", "spawnvp"), ("os", "posix_spawn"), ("asyncio", "create_subprocess_exec"),
    ("asyncio", "create_subprocess_shell"), ("multiprocessing", "Process"),
}
# 終わるまで待つ呼び出し（OS の道具を流すのには使ってよい。アプリ本体を起動してはいけない）
WAITING_RUNNERS = {"run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}


def app_test_files() -> list[Path]:
    return sorted(TESTS.glob("test_app_*.py"))


def calls_in(path: Path) -> list[tuple[int, str, str, str]]:
    """（行、持ち主、名前、呼び出しの文）。持ち主は a.b(…) の a（無ければ空）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                found.append((node.lineno, ast.unparse(func.value), func.attr, ast.unparse(node)))
            elif isinstance(func, ast.Name):
                found.append((node.lineno, "", func.id, ast.unparse(node)))
    return found


def test_app_tests_start_processes_only_through_the_tracking_helper():
    """アプリの試験は、プロセスを tests/app_procs.py（spawn、Launched）を通して起動する。

    - subprocess.Popen などを直接は呼ばない（呼ぶと、控えに入らず、止め忘れが残る）
    - 終わるまで待つ呼び出し（subprocess.run など。OS の道具に使う）で、アプリ本体を起動しない
    - 部品を使う試験ファイルには、どの試験にも掛かる見張り（autouse のフィクスチャが reap を呼ぶ）がある
    構文木で見るので、名前を付け替えた呼び方（from subprocess import Popen など）も、名前で拾う。"""
    files = app_test_files()
    assert len(files) >= 4, files
    problems = []
    users = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for line, owner, name, text in calls_in(path):
            where = f"{path.name}:{line}"
            if (owner.split(".")[-1], name) in DIRECT_STARTERS or (owner == "" and name == "Popen"):
                problems.append(f"{where}: 部品を通さない起動 {owner}.{name}(…)")
            if owner.split(".")[-1] == "subprocess" and name in WAITING_RUNNERS and "circuit_sim" in text:
                problems.append(f"{where}: 終わるまで待つ呼び出しで、アプリ本体を起動している")
        imports_helper = any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and "app_procs" in ast.unparse(node)
            for node in ast.walk(tree)
        )
        if not imports_helper:
            continue
        users.append(path.name)
        watched = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            reaps = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "reap"
                for c in ast.walk(node)
            )
            function_scope = "scope=" not in decorators or "scope='function'" in decorators
            if "fixture" in decorators and "autouse=True" in decorators and reaps and function_scope:
                watched = True
        if not watched:
            problems.append(f"{path.name}: 部品を使うのに、試験ごとの見張り（autouse で reap を呼ぶ）が無い")
    assert not problems, "\n".join(problems)
    # アプリ本体を起動する試験ファイルは、どれも部品を使っている
    assert {"test_app_cli.py", "test_app_browser.py", "test_app_config.py"} <= set(users), users


# わざと残す試験（別の pytest で流す）。起動と見張りは、実装者のもの
# （tests/test_app_cli.py、tests/app_procs.py）
LEAKING_TESTS = r'''
import os
import socket

import app_procs
from test_app_cli import launch, no_process_left_behind, read_lines_until_url, run_to_end  # noqa: F401

OUT = os.environ["A0V6_OUT"]


def note(name, pid):
    with open(os.path.join(OUT, name + ".pid"), "w", encoding="utf-8") as f:
        f.write(str(pid))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_started_when_it_should_have_failed(tmp_path, monkeypatch):
    """「起動に失敗して終わるはず」の起動が、起動に成功する（結べるポートを渡す）。"""
    real = app_procs.spawn

    def spawn(cmd, **kwargs):
        proc = real(cmd, **kwargs)
        note("started", proc.pid)
        return proc

    monkeypatch.setattr(app_procs, "spawn", spawn)
    run_to_end("--no-browser", "--port", str(free_port()), config=tmp_path, timeout=3)


def test_exception_in_the_middle(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    note("middle", proc.pid)
    read_lines_until_url(proc)
    raise RuntimeError("left in the middle")


def test_timeout_while_waiting_for_a_line(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    note("timeout", proc.pid)
    read_lines_until_url(proc)
    proc.wait_line("this line is never printed", timeout=2)


def test_clean_run_is_not_blamed(tmp_path):
    code, out = run_to_end("--no-browser", "--help", config=tmp_path)
    assert code == 0, out
'''

# ハーネスに止められるまで、アプリ本体を起動したまま待つ試験（別の pytest で流す）
SLEEPING_TEST = r'''
import os
import time

from test_app_cli import launch, no_process_left_behind, read_lines_until_url  # noqa: F401

OUT = os.environ["A0V6_OUT"]


def test_sleeps_with_an_app_running(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    try:
        read_lines_until_url(proc)
        with open(os.path.join(OUT, "app.part"), "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
        os.replace(os.path.join(OUT, "app.part"), os.path.join(OUT, "app.pid"))
        time.sleep(120)
    finally:
        proc.stop()
'''


def inner_run(tmp_path: Path, source: str) -> tuple[list[str], dict[str, str], Path]:
    """別の pytest で source を流すコマンドと環境変数（実装者の試験の部品を、そのまま読み込める形）。"""
    place = tmp_path / "inner"
    out = tmp_path / "out"
    place.mkdir()
    out.mkdir()
    (place / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (place / "test_inner.py").write_text(source, encoding="utf-8")
    paths = [str(ROOT), str(TESTS)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "PYTHONIOENCODING": "utf-8",
           "PYTHONUTF8": "1", "A0V6_OUT": str(out)}
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "test_inner.py", "-q", "-p", "no:cacheprovider",
               "-c", str(place / "pytest.ini"), f"--rootdir={place}", f"--basetemp={tmp_path / 'inner-tmp'}"]
    return command, env, out


def running_command(pid: int) -> str | None:
    """pid が動いていれば、そのコマンドライン。動いていなければ None。一覧を作れなければ、試験をスキップ。"""
    listed = watch.list_processes()
    if listed is None:
        pytest.skip("プロセスの一覧を作れないので、残ったプロセスを確かめられなかった")
    for process in listed[0]:
        if process.pid == pid:
            return process.command
    return None


def gone_or_killed(pid: int, wait: float = 5.0) -> str | None:
    """pid のアプリ本体が wait 秒のうちに終わっていれば None。動いていたら、止めて、コマンドラインを返す。

    止めるのは、コマンドラインがアプリ本体のもの（circuit_sim）だけ（番号が使い回されていても、ほかの
    プロセスを止めない）。"""
    deadline = time.monotonic() + wait
    while True:
        command = running_command(pid)
        if command is None or "circuit_sim" not in command:
            return None
        if time.monotonic() > deadline:
            watch.kill_pid(pid)
            return command
        time.sleep(0.2)


def test_leaked_processes_are_stopped_and_their_tests_fail(tmp_path):
    """実装者の部品と見張りは、残したプロセスを止めて、残した試験を失敗にする。

    - 「起動に失敗して終わるはず」が起動に成功した: run_to_end が、止めてから落とす（PO の PC で起きた形）
    - 途中で例外で抜けた、出力を待つ時間が切れた: 見張り（no_process_left_behind）が、試験の終わりに止めて、
      その試験をエラーにする
    - 残していない試験は、巻き添えにならない
    Windows では、止める道は taskkill /F /T（tests/app_procs.py の kill_tree）。"""
    command, env, out = inner_run(tmp_path, LEAKING_TESTS)
    done = subprocess.run(command, cwd=tmp_path / "inner", env=env, capture_output=True, timeout=300,
                          stdin=subprocess.DEVNULL, check=False)
    text = (done.stdout + done.stderr).decode("utf-8", "replace")
    summary = text.strip().splitlines()[-1] if text.strip() else ""
    pids = {path.stem: int(path.read_text(encoding="utf-8")) for path in sorted(out.glob("*.pid"))}
    left = {name: command for name, pid in pids.items() if (command := gone_or_killed(pid)) is not None}
    assert set(pids) == {"started", "middle", "timeout"}, (pids, text[-3000:])
    assert not left, f"試験が残したアプリ本体が、試験の後も動いていた（止めた）: {left}\n{text[-3000:]}"
    for wanted in ("3 failed", "1 passed", "2 errors"):
        assert re.search(rf"\b{wanted}\b", summary), (
            f"残した試験が、失敗になっていない（終わりの行: {summary!r}）\n{text[-3000:]}"
        )
    assert done.returncode == 1, (done.returncode, summary)


def load_harness():
    """ハーネス（scripts/cycle.py）を、モジュールとして読み込む。"""
    spec = importlib.util.spec_from_file_location("a0v6_cycle_script", ROOT / "scripts" / "cycle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(WINDOWS, reason="POSIX のプロセスのまとまりの話。Windows は taskkill /T が親子をたどる")
@pytest.mark.xfail(strict=True, reason=(
    "軽微（A0 の検証 6回目の問題）: tests/app_procs.py の spawn が、起動するものを新しいまとまり"
    "（start_new_session）に入れるので、ハーネスが確認をまとまりごと止めても、アプリ本体が残る"
))
def test_apps_started_by_the_app_tests_die_when_the_harness_kills_the_check(tmp_path):
    """ハーネスが確認（pytest）を止めたら、その試験が起動したアプリ本体も止まる（POSIX）。

    scripts/local_ci.py は、確認を cycle.group_options()（新しいまとまり）で起動し、時間切れと中断
    （Ctrl-C、終了の合図）のときに cycle.kill_tree（まとまりごと SIGKILL）で止める。pytest は後始末を
    する間もなく終わるので、試験の見張りは働かない。起動したアプリ本体が同じまとまりにいれば一緒に止まる
    （22d2fdc の試験は、そうだった）。別のまとまりにいると、待ち受けたまま残る。"""
    cycle = load_harness()
    command, env, out = inner_run(tmp_path, SLEEPING_TEST)
    check = subprocess.Popen(command, cwd=tmp_path / "inner", env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **cycle.group_options())
    pid = None
    try:
        deadline = time.monotonic() + 120
        while not (out / "app.pid").exists():
            assert check.poll() is None, "確認が、アプリ本体を起動する前に終わった"
            assert time.monotonic() < deadline, "確認が、アプリ本体を起動しない"
            time.sleep(0.1)
        pid = int((out / "app.pid").read_text(encoding="utf-8"))
        cycle.kill_tree(check)      # 時間切れ・中断のときの、ハーネスの止め方
        check.wait(timeout=30)
        left = gone_or_killed(pid)
        pid = None
        assert left is None, (
            f"ハーネスが確認を止めた後も、試験が起動したアプリ本体が動いていた（止めた）: {left}"
        )
    finally:
        if check.poll() is None:
            try:
                os.killpg(check.pid, signal.SIGKILL)
            except OSError:
                pass
            check.kill()
            check.wait(timeout=30)
        if pid is not None:
            gone_or_killed(pid, wait=0.0)
