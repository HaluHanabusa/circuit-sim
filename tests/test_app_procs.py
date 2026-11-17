"""アプリの試験の部品（tests/app_procs.py）の試験: 起動したものを残さない。控えが、パイプを持ち続けない。

アプリ本体の代わりに、眠るだけの Python（引数に、試験ごとの印を持つ）を起動する。残ったかどうかは、
プロセスの一覧（ps）で印を探して見る。見つけたものは、試験の終わりに止める（印は試験ごとに違うので、
ほかの人のプロセスを止めない）。
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import app_procs as procs
import pytest

WINDOWS = os.name == "nt"
TESTS = Path(__file__).resolve().parent

# 印を引数に持って、眠るだけのプロセス。起きたら「up 自分の番号」を出す
SLEEPER = "import os, sys, time\nprint('up', os.getpid(), flush=True)\ntime.sleep(120)\n"

# 子を2つ起動してから眠る親（引数: 印）。コードは環境変数で受け渡す（PARENT_ENV）:
# - 同じまとまりの子（webbrowser が BROWSER のコマンドを、終わりを待つ形で起動するときと同じ）
# - 自分だけのまとまりに移る子（起動役）。その子が、さらに孫を起動する（終わりに & を付けた BROWSER の
#   コマンドを、webbrowser が start_new_session=True で起動するときと同じ）
PARENT = """\
import os, subprocess, sys, time

mark = sys.argv[1]
subprocess.Popen([sys.executable, "-c", os.environ["A0_PROCS_SLEEPER"], mark])
if sys.argv[2:] != ["spawner"]:
    subprocess.Popen([sys.executable, "-c", os.environ["A0_PROCS_PARENT"], mark, "spawner"],
                     start_new_session=True)
print("up", sys.argv[2:] or "parent", flush=True)
time.sleep(120)
"""
PARENT_ENV = {"A0_PROCS_SLEEPER": SLEEPER, "A0_PROCS_PARENT": PARENT}

# ハーネスに止められる確認（pytest）の身代わり: 部品で2つ起動し（ふつうの起動と、新しいまとまりの起動）、
# それぞれの番号を出して眠る。印は環境変数 A0_PROCS_MARK
CHECK = f"""\
import os, sys, time

sys.path.insert(0, {str(TESTS)!r})
import app_procs

mark = os.environ["A0_PROCS_MARK"]
plain = app_procs.spawn([sys.executable, "-c", {SLEEPER!r}, mark])
session = app_procs.spawn([sys.executable, "-c", {SLEEPER!r}, mark], new_session=True)
print("ready", plain.pid, session.pid, flush=True)
time.sleep(120)
"""


@pytest.fixture(autouse=True)
def no_process_left_behind():
    """その試験が起動したプロセスが、試験の終わりに動いていたら、止めて（子も）、その試験を失敗にする。"""
    start = procs.mark()
    yield
    left = procs.reap(start)
    if left:
        pytest.fail("この試験が起動したプロセスが、試験の終わりにも動いていた（止めた）:\n"
                    + "".join(f"  {line}\n" for line in left), pytrace=False)


def marked(token: str) -> list[int]:
    """引数に token を持つ、動いているプロセスの番号（POSIX。ps で探す）。"""
    done = subprocess.run(["ps", "-A", "-o", "pid=,stat=,args="], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        pytest.skip("ps が使えないので、残ったプロセスを確かめられない")
    found = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and not parts[1].startswith("Z") and token in parts[2]:
            found.append(int(parts[0]))
    return [pid for pid in found if pid != os.getpid()]


def gone_within(token: str, seconds: float) -> list[int]:
    """seconds 秒のうちに、印のプロセスが無くなれば []。残っていれば、その番号。"""
    deadline = time.monotonic() + seconds
    while True:
        left = marked(token)
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.1)


@pytest.fixture
def token():
    """試験ごとの印。試験の終わりに、印のプロセスが残っていれば止める（PC に残さない）。"""
    value = f"a0-procs-{secrets.token_hex(8)}"
    yield value
    if not WINDOWS:
        for pid in marked(value):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


only_posix = pytest.mark.skipif(WINDOWS, reason="POSIX のプロセスのまとまりの話（Windows は taskkill /T）")


@only_posix
def test_started_processes_die_when_the_harness_kills_the_check_with_its_group(tmp_path, token):
    """ハーネスが確認をまとまりごと止めても（scripts/cycle.py の kill_tree: まとまりごと SIGKILL）、
    確認が部品で起動したものは残らない。

    - ふつうの起動（spawn）: 確認と同じまとまりにいるので、一緒に止まる
    - 新しいまとまりの起動（spawn(new_session=True)。疑似端末の試験が使う）: 見張りが、確認のプロセスが
      終わったことに気づいて止める
    確認の身代わりは、scripts/local_ci.py と同じく、新しいまとまりの先頭で起動する（spawn の
    new_session=True。こちらの見張りは、この試験のプロセスが終わったときのためのもの）。"""
    check = procs.Launched([sys.executable, "-c", CHECK], cwd=tmp_path,
                           env={**os.environ, "A0_PROCS_MARK": token}, new_session=True)
    try:
        line = check.wait_line("ready", timeout=30)
        started = [int(word) for word in line.split()[1:]]
        deadline = time.monotonic() + 30
        while len(running := marked(token)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)  # 新しいまとまりの起動が、見張りを分けるまで
        assert set(started) <= set(running) and len(running) == 3, (line, running)  # 2つと、見張り
        os.killpg(check.pid, signal.SIGKILL)  # ハーネスの止め方（時間切れ、中断）
        check.wait(timeout=30)
        left = gone_within(token, 5)
        assert not left, f"確認をまとまりごと止めた後も、確認が起動したものが動いていた: {left}（{started}）"
    finally:
        check.stop()


@only_posix
def test_kill_tree_stops_children_and_grandchildren_while_the_parent_runs(tmp_path, token):
    """kill_tree は、親が動いている間なら、子と孫も止める。自分だけのまとまりに移った子と、その孫も。

    前の止め方（起動したもののまとまりごと止める）では、自分だけのまとまりに移った子は残った。"""
    parent = procs.Launched([sys.executable, "-c", PARENT, token], cwd=tmp_path,
                            env={**os.environ, **PARENT_ENV})
    try:
        ups = [parent.wait_line("up", timeout=30) for _ in range(4)]  # 親、子、起動役、孫
        assert len(marked(token)) == 4, (ups, marked(token))
        procs.kill_tree(parent.proc)
        left = gone_within(token, 5)
        assert not left, f"親を止めた後も、子か孫が動いていた: {left}"
    finally:
        parent.stop()


@only_posix
def test_a_new_session_keeps_the_number_and_its_guard_ends_with_it(tmp_path, token):
    """新しいまとまりの起動: 番号（pid）は本来のコマンドのもの。まとまりの先頭にいる。

    本来のコマンドがふつうに終わったら、見張りも終わる（試験のプロセスが動いていても）。"""
    proc = procs.Launched([sys.executable, "-c", SLEEPER, token], cwd=tmp_path, env=dict(os.environ),
                          new_session=True)
    try:
        line = proc.wait_line("up", timeout=30)
        assert int(line.split()[1]) == proc.pid
        assert os.getsid(proc.pid) == proc.pid and os.getpgid(proc.pid) == proc.pid
        assert len(marked(token)) == 2, marked(token)  # 本来のコマンドと、見張り
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == -signal.SIGTERM
        left = gone_within(token, 5)
        assert not left, f"本来のコマンドが終わった後も、見張りが動いていた: {left}"
    finally:
        proc.stop()


@only_posix
def test_terminal_signals_do_not_reach_the_guard(tmp_path, token):
    """見張りは、自分だけのまとまりにいる（本来のコマンドのまとまりに送られる端末の合図を受けない）。"""
    proc = procs.Launched([sys.executable, "-c", SLEEPER, token], cwd=tmp_path, env=dict(os.environ),
                          new_session=True)
    try:
        proc.wait_line("up", timeout=30)
        guards = [pid for pid in marked(token) if pid != proc.pid]
        assert len(guards) == 1, marked(token)
        assert os.getpgid(guards[0]) != os.getpgid(proc.pid)
        os.killpg(proc.pid, signal.SIGINT)  # 端末の Ctrl+C と同じ届き方
        assert proc.wait(timeout=10) != 0
        assert not gone_within(token, 5)
    finally:
        proc.stop()


def test_spawn_does_not_make_a_new_group_without_the_guard():
    """まとまりの指定は、spawn では受けない（見張りの無い新しいまとまりを作らない）。"""
    for key, value in (("start_new_session", True), ("process_group", 0), ("preexec_fn", os.getpid)):
        with pytest.raises(ValueError, match=key):
            procs.spawn([sys.executable, "-c", "pass"], **{key: value})
    if not WINDOWS:
        proc = procs.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert os.getpgid(proc.pid) == os.getpgid(0), "ふつうの起動が、試験と別のまとまりにいる"
        finally:
            procs.kill_tree(proc)


def test_reap_lets_go_of_ended_processes_and_closes_what_it_held():
    """reap は、確かめたもののうち終わっているものを控えから外し、標準入力のパイプを閉じる。

    keep のものと、動いているものは外さない（keep のものは止めもしない）。控えは、試験の数に比例して
    増えない（前は、終わったプロセスの持ち物を、実行の終わりまで持ち続けていた。レビュー 6回目の軽微7）。"""
    start = procs.mark()
    ended = procs.spawn([sys.executable, "-c", "pass"], stdin=subprocess.PIPE)
    kept = procs.spawn([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE)
    try:
        ended.wait(timeout=30)
        assert procs.reap(start, keep={kept.pid}) == []
        assert ended not in procs.STARTED, "終わったものを、控えから外していない"
        assert ended.stdin.closed, "終わったものの標準入力のパイプを、閉じていない"
        assert kept in procs.STARTED and kept.poll() is None and not kept.stdin.closed
        left = procs.reap(start)
        assert len(left) == 1 and str(kept.pid) in left[0]
        assert kept not in procs.STARTED and kept.poll() is not None and kept.stdin.closed
    finally:
        procs.kill_tree(kept)


def test_launched_closes_its_output_once_it_has_read_it_all(tmp_path):
    """Launched は、出力を終わりまで読んだら閉じる（読むのは、そのスレッドだけ）。"""
    proc = procs.Launched([sys.executable, "-c", "print('done')"], cwd=tmp_path, env=dict(os.environ))
    try:
        code, out = proc.end(30)
        assert (code, out.strip()) == (0, "done")
        assert proc.proc.stdout.closed
    finally:
        proc.stop()
