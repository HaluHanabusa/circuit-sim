"""A0 の独立検証（verifier、7回目）: アプリの試験の部品（tests/app_procs.py）の止め方と、手で確かめる
項目の書き方。

記録は docs/verification/A0-verifier-7.md。6回目の問題2（ハーネスが確認をまとまりごと止めると、試験が起動した
アプリ本体が残る）を、実装者は「ふつうの起動は試験のプロセスと同じまとまり、新しいまとまりが要る起動
（疑似端末）は、試験のプロセスが終わったら止める見張り（命綱のパイプ）つき」で直した（91213f3）。
6回目の受け入れ試験（test_a0_fix6_test_hygiene.py）は、ふつうの起動だけを見る。ここでは残りを確かめる。

1. ハーネスと同じ起動・止め方（scripts/cycle.py の group_options と kill_tree）で確認を止めたとき、
   ふつうの起動、新しいまとまりの起動（端末なし。命綱の見張りだけが止める道）、疑似端末の起動の3つとも、
   アプリ本体も見張りも残らない（POSIX）。
2. kill_tree は、止める木の外（兄弟の木、木の外のプロセス）を止めない・一時停止したままにしない
   （「ほかの人のプロセスを止めない」の確かめ。番号の使い回しそのものは起こせないので、木の外に触らない
   ことを見る）。
3. 起動と後始末を何回繰り返しても、試験のプロセスの開いている fd が増えない（レビュー 6回目の軽微7）。
4. 設計書 8.5 の「BROWSER にコマンドを指定した起動」の手順のコマンド:
   - そのコマンドが、webbrowser の分け方（shlex）で分けて起動すると、opened.txt に場所を書く（どの OS でも）
   - 値が ; を含まない（後退を見張る試験）。Windows の webbrowser は、BROWSER を os.pathsep（Windows
     では ;）で区切って、いくつかの候補として読む（CPython 3.13 の Lib/webbrowser.py の
     register_standard_browsers: os.environ["BROWSER"].split(os.pathsep)）。7回目の手順の値は Python の
     文の区切りに ; を使っていたので、Windows では2つに分かれ、コマンドは始まらなかった（7回目の問題3。
     8aea404 で直った。8回目の検証で xfail(strict) の印を外した。webbrowser を通した確かめは
     test_a0_fix8_procs_browser.py）。

止める確かめで送る本物の合図は、印（引数か環境変数）で確かめた、この試験が起動したものにだけ届くようにする
（D-078。2 は、許す番号の一覧を持つ gate を os.kill に差し替える。1 の後始末は、Linux では印も確かめる）。
"""
from __future__ import annotations

import contextlib
import errno
import importlib.util
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import a0_marked as marked
import acceptance_watch as watch
import app_procs as procs
import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
DESIGN = ROOT / "docs" / "design" / "app-design.md"

only_posix = pytest.mark.skipif(WINDOWS, reason="POSIX のプロセスのまとまりの話（Windows は taskkill /T）")


# ---------------------------------------------------------------------------------------------------------
# 1. ハーネスが確認を止めたとき、3つの起動のどれも残らない
# ---------------------------------------------------------------------------------------------------------

# 別の pytest で流す試験。起動と見張りは実装者のもの（tests/test_app_cli.py、tests/app_procs.py）。
# 3つのアプリ本体を起動して、番号を書き、ハーネスに止められるまで眠る。
HOLDING_TEST = r'''
import os
import sys
import time

import app_procs
from test_app_cli import (  # noqa: F401
    ROOT, app_env, launch, launch_on_a_tty, no_process_left_behind, read_lines_until_url,
)

OUT = os.environ["A0V7_OUT"]


def test_holds_three_apps(tmp_path):
    for name in ("plain", "alone", "tty"):
        (tmp_path / name).mkdir()
    plain = launch("--no-browser", config=tmp_path / "plain")
    read_lines_until_url(plain)
    # 新しいまとまりで、端末なし（端末が閉じたときの SIGHUP は届かない。命綱の見張りだけが止める）
    alone = app_procs.Launched([sys.executable, "-m", "circuit_sim.app", "--no-browser"], cwd=ROOT,
                               env=app_env(tmp_path / "alone"), new_session=True)
    read_lines_until_url(alone)
    tty, master = launch_on_a_tty(tmp_path / "tty")
    read_lines_until_url(tty)
    with open(os.path.join(OUT, "apps.part"), "w", encoding="utf-8") as f:
        f.write(f"{plain.pid} {alone.pid} {tty.pid}")
    os.replace(os.path.join(OUT, "apps.part"), os.path.join(OUT, "apps.pid"))
    time.sleep(120)
'''


def inner_run(tmp_path: Path, source: str, mark: str) -> tuple[list[str], dict[str, str], Path]:
    """別の pytest で source を流すコマンドと環境変数（実装者の試験の部品を、そのまま読み込める形）。

    環境変数 A0V7_MARK に印を入れる（起動されたものは、どれも受け継ぐ。Linux では、残ったものを印で探す）。"""
    place = tmp_path / "inner"
    out = tmp_path / "out"
    place.mkdir()
    out.mkdir()
    (place / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (place / "test_inner.py").write_text(source, encoding="utf-8")
    paths = [str(ROOT), str(TESTS)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "PYTHONIOENCODING": "utf-8",
           "PYTHONUTF8": "1", "A0V7_OUT": str(out), "A0V7_MARK": mark}
    env.pop("PYTEST_ADDOPTS", None)
    command = [sys.executable, "-m", "pytest", "test_inner.py", "-q", "-p", "no:cacheprovider",
               "-c", str(place / "pytest.ini"), f"--rootdir={place}", f"--basetemp={tmp_path / 'inner-tmp'}"]
    return command, env, out


def load_harness():
    """ハーネス（scripts/cycle.py）を、モジュールとして読み込む。"""
    spec = importlib.util.spec_from_file_location("a0v7_cycle_script", ROOT / "scripts" / "cycle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def running_command(pid: int) -> str | None:
    """pid が動いていれば、そのコマンドライン。動いていなければ None。一覧を作れなければ、試験をスキップ。"""
    listed = watch.list_processes()
    if listed is None:
        pytest.skip("プロセスの一覧を作れないので、残ったプロセスを確かめられなかった")
    for process in listed[0]:
        if process.pid == pid:
            return process.command
    return None


def marked_pids(mark: str) -> list[int]:
    """Linux: 環境変数 A0V7_MARK=mark を持つ、動いているプロセス（ゾンビを除く）。ほかの OS は []。"""
    if not LINUX:
        return []
    wanted = f"A0V7_MARK={mark}".encode()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            state = Path(f"/proc/{name}/stat").read_text(encoding="utf-8", errors="replace")
            if state.rsplit(")", 1)[1].split()[0] == "Z":
                continue
            if wanted in Path(f"/proc/{name}/environ").read_bytes().split(b"\0"):
                found.append(int(name))
        except (OSError, IndexError):
            continue
    return found


def left_after(pids: list[int], mark: str, wait: float = 5.0) -> list[str]:
    """wait 秒のうちに、pids のアプリ本体と、印のあるプロセスが無くなれば []。残っていたら止めて、説明を返す。

    止めるのは、コマンドラインがアプリ本体のもの（circuit_sim）か、この試験の印を持つものだけ（番号が
    使い回されていても、ほかのプロセスを止めない）。Linux では、アプリ本体も、環境変数の印（A0V7_MARK）を
    止める直前に確かめる（D-078。レビュー 8回目の軽微3。8回目の検証で足した）。"""
    deadline = time.monotonic() + wait
    entry = f"A0V7_MARK={mark}"
    while True:
        apps = {pid: command for pid in pids
                if (command := running_command(pid)) is not None and "circuit_sim" in command}
        others = [pid for pid in marked_pids(mark) if pid not in apps]
        if not apps and not others:
            return []
        if time.monotonic() > deadline:
            left = [f"PID {pid}: {watch.shown(command)}" for pid, command in apps.items()]
            left += [f"PID {pid}: 印のあるプロセス" for pid in others]
            for pid in apps:
                if LINUX:
                    marked.kill_if_marked(pid, environ=entry, command="circuit_sim")
                else:
                    watch.kill_pid(pid)
            for pid in others:
                marked.kill_if_marked(pid, environ=entry)
            return left
        time.sleep(0.2)


@only_posix
def test_no_app_is_left_when_the_harness_kills_the_check_whatever_the_launch(tmp_path):
    """ハーネスが確認（pytest）をまとまりごと止めたら、その試験が起動したアプリ本体は、起動の形によらず止まる。

    - ふつうの起動（launch）: 確認と同じまとまりにいるので、一緒に止まる
    - 新しいまとまりの起動で、端末なし（spawn(new_session=True)）: 命綱の見張りが止める（ほかに止める道は
      無い）
    - 疑似端末の起動（launch_on_a_tty）: 見張り、または端末が閉じたときの合図で止まる
    見張り（命綱の見張りのプロセス）も残らない（Linux では、環境変数の印で探す）。
    6回目の受け入れ試験は、ふつうの起動だけを見ていた。"""
    cycle = load_harness()
    mark = f"a0v7-{secrets.token_hex(8)}"
    command, env, out = inner_run(tmp_path, HOLDING_TEST, mark)
    check = subprocess.Popen(command, cwd=tmp_path / "inner", env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **cycle.group_options())
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 120
        while not (out / "apps.pid").exists():
            assert check.poll() is None, "確認が、アプリ本体を起動する前に終わった"
            assert time.monotonic() < deadline, "確認が、アプリ本体を起動しない"
            time.sleep(0.1)
        pids = [int(word) for word in (out / "apps.pid").read_text(encoding="utf-8").split()]
        assert len(pids) == 3, pids
        if LINUX:
            # 起動したものが、どれも印を受け継いでいる（印で探す確かめが、空振りしていない）
            assert set(pids) <= set(marked_pids(mark)), (pids, marked_pids(mark))
        cycle.kill_tree(check)      # 時間切れ・中断のときの、ハーネスの止め方
        check.wait(timeout=30)
        launched, pids = pids, []
        left = left_after(launched, mark)
        assert not left, (
            "ハーネスが確認を止めた後も、試験が起動したもの（ふつう、新しいまとまり、疑似端末の順の番号 "
            f"{launched}）が動いていた（止めた）:\n" + "\n".join(left)
        )
    finally:
        if check.poll() is None:
            try:
                os.killpg(check.pid, signal.SIGKILL)
            except OSError:
                pass
            check.kill()
            check.wait(timeout=30)
        if pids:
            left_after(pids, mark, wait=0.0)


# ---------------------------------------------------------------------------------------------------------
# 2. kill_tree は、止める木の外に触らない
# ---------------------------------------------------------------------------------------------------------

# 子を2つ起動してから眠る親（引数: 印）。1つは同じまとまりの子、1つは自分だけのまとまりに移る子で、その子が
# 孫を起動する。どれも引数に印を持つ。起動したものの番号を「子 起動役 孫」の順に1行で出す
TREE = """\
import subprocess, sys, time

mark = sys.argv[1]
sleeper = [sys.executable, "-c", "import time; time.sleep(120)", mark]
child = subprocess.Popen(sleeper)
spawner = subprocess.Popen(
    [sys.executable, "-c",
     "import subprocess, sys, time; p = subprocess.Popen(%r); print(p.pid, flush=True); time.sleep(120)"
     % sleeper, mark],
    stdout=subprocess.PIPE, text=True, start_new_session=True)
grandchild = int(spawner.stdout.readline())
print(child.pid, spawner.pid, grandchild, flush=True)
time.sleep(120)
"""

REAL_KILL = os.kill
REAL_KILLPG = os.killpg if hasattr(os, "killpg") else None


class Gate:
    """os.kill / os.killpg の差し替え（D-078。レビュー 8回目の中1。8回目の検証で足した）。

    許す番号（印と、木の親が出した行で確かめた、この試験が起動したもの）にだけ、本物の合図を送る。ほかの
    番号への送信と、まとまりへの送信は、送らずに控えて、ProcessLookupError（ESRCH）を出す。部品の止め方が
    後退しても、木の外に本物の合図は届かず、どの番号に送ろうとしたかが控えに残る。"""

    def __init__(self, allowed) -> None:
        self.allowed = frozenset(allowed)
        self.sent: list[tuple[int, int]] = []
        self.refused: list[tuple] = []

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.allowed:
            self.refused.append((pid, int(sig)))
            raise ProcessLookupError(errno.ESRCH, "gate: 許す番号の外には送らない")
        self.sent.append((pid, int(sig)))
        REAL_KILL(pid, sig)

    def killpg(self, pgid: int, sig: int) -> None:
        self.refused.append(("group", pgid, int(sig)))
        raise ProcessLookupError(errno.ESRCH, "gate: まとまりには送らない")


@contextlib.contextmanager
def gated(allowed):
    """その間だけ、os.kill と os.killpg（tests/app_procs.py も Popen.kill も使う）を Gate に替える。"""
    gate = Gate(allowed)
    procs.os.kill, procs.os.killpg = gate.kill, gate.killpg
    try:
        yield gate
    finally:
        procs.os.kill, procs.os.killpg = REAL_KILL, REAL_KILLPG


def state_of(pid: int) -> str | None:
    """Linux の /proc の状態（S、R、T など）。無ければ None。ゾンビは None と同じに扱う。"""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    state = text.rsplit(")", 1)[1].split()[0]
    return None if state == "Z" else state


def has_mark(pid: int, mark: str, wait: float = 10.0) -> bool:
    """Linux: pid のプロセスが、引数に mark を持つ（ゾンビと、無いものは偽）。

    起動の直後は、まだ実行ファイルの入れ替えの途中で、引数が空に読めることがある（Linux で実測）ので、
    wait 秒まで待つ。"""
    deadline = time.monotonic() + wait
    while True:
        try:
            if mark.encode() in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"):
                return True
        except OSError:
            return False
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def start_tree(tmp_path: Path, mark: str, allowed: list[int]) -> tuple[subprocess.Popen, list[int]]:
    """TREE を起動し、（親、[親、子、起動役、孫] の番号）を返す。親は起動した直後に、ほかは親が出した行の
    番号を印で確かめてから、allowed に足す（後始末で止めてよい番号）。"""
    root = procs.spawn([sys.executable, "-c", TREE, mark], cwd=tmp_path, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, text=True)
    allowed.append(root.pid)  # 受け取る前のこの試験の子（番号は使い回されない）
    line = root.stdout.readline()
    members = [root.pid, *(int(word) for word in line.split())]
    assert len(members) == 4 and all(has_mark(pid, mark) for pid in members), (
        "木の番号を、印で確かめられない", line, members)
    allowed.extend(members[1:])
    return root, members


@pytest.mark.skipif(not LINUX, reason="プロセスの状態（一時停止しているか）を /proc で見る（Linux だけ）")
def test_kill_tree_stops_only_its_own_tree(tmp_path):
    """kill_tree(A) は、A とその子・孫（自分だけのまとまりに移った子の先も）を止める。木の外の、兄弟の
    木 B と、別に起動したプロセスは、止めない。一時停止（SIGSTOP）したままにもしない。

    止め方は「親を一時停止してから子と孫をたどって止める」（tests/app_procs.py の kill_posix_tree）。止める
    のは動いている親からたどれる番号だけで、木の外の番号には合図を送らない、ということを、os.kill の gate
    （許すのは A の木の番号だけ）の控えと、木の外のプロセスの状態で見る。gate があるので、部品が後退しても、
    本物の合図は木の外に届かない（D-078）。後始末も gate の中で行う（許すのは、この試験が起動したもの
    だけ）。"""
    mark = f"a0v8-tree-{secrets.token_hex(8)}"
    trees = []
    allowed: list[int] = []
    outside = procs.spawn([sys.executable, "-c", "import time; time.sleep(120)", mark],
                          stdin=subprocess.DEVNULL)
    allowed.append(outside.pid)
    try:
        a, a_members = start_tree(tmp_path, mark, allowed)
        trees.append(a)
        b, b_members = start_tree(tmp_path, mark, allowed)
        trees.append(b)
        assert all(state_of(pid) for pid in a_members + b_members), (a_members, b_members)
        with gated(a_members) as gate:
            procs.kill_tree(a)
        assert not gate.refused, ("kill_tree(A) が、A の木の外に合図を送ろうとした（gate が止めた）",
                                  gate.refused, a_members)
        killed = {pid for pid, sig in gate.sent if sig == signal.SIGKILL}
        assert killed == set(a_members), ("kill_tree(A) が、A の木のどれかに SIGKILL を送っていない",
                                          gate.sent, a_members)
        deadline = time.monotonic() + 5
        while any(state_of(pid) for pid in a_members) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not [pid for pid in a_members if state_of(pid)], (
            "kill_tree の後も、木の中のプロセスが動いていた", {pid: state_of(pid) for pid in a_members})
        untouched = {pid: state_of(pid) for pid in [*b_members, outside.pid]}
        assert all(state in ("S", "R", "D") for state in untouched.values()), (
            "木の外のプロセスが、止まったか一時停止している（T）", untouched)
    finally:
        with gated(allowed) as cleanup:
            for proc in [*trees, outside]:
                procs.kill_tree(proc)
                if proc.stdout is not None:
                    proc.stdout.close()
    assert not cleanup.refused, ("後始末の kill_tree が、この試験の起動したものの外に合図を送ろうとした",
                                 cleanup.refused)


# ---------------------------------------------------------------------------------------------------------
# 3. 起動と後始末を繰り返しても、開いている fd が増えない
# ---------------------------------------------------------------------------------------------------------

def open_fd_count() -> int:
    for place in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(place))
        except OSError:
            continue
    pytest.skip("開いている fd を数えられない")


@pytest.mark.skipif(WINDOWS, reason="開いている fd を /proc か /dev/fd で数える（POSIX だけ）")
def test_launching_and_reaping_many_times_does_not_pile_up_open_pipes(tmp_path):
    """起動（Launched、標準入力をパイプにした spawn）と後始末（reap）を何回繰り返しても、試験のプロセスの
    開いている fd は増えない。控えにも残らない。

    6回目の検証では、tests/test_app_cli.py と test_app_config.py を流した終わりに、控えに 67 個、開いている
    fd が 54 個あった（終わったプロセスのパイプを、控えが持ち続けていた。レビュー 6回目の軽微7）。
    ここでは、繰り返しの回数（40）に比べて十分に小さい余裕（2）だけを許す。"""
    start = procs.mark()
    before = open_fd_count()
    for _ in range(30):
        launched = procs.Launched([sys.executable, "-c", "print('x')"], cwd=tmp_path, env=dict(os.environ))
        assert launched.end(30) == (0, "x\n")
    for _ in range(10):
        piped = procs.spawn([sys.executable, "-c", "pass"], stdin=subprocess.PIPE)
        piped.wait(timeout=30)
    assert procs.reap(start) == []
    after = open_fd_count()
    assert after <= before + 2, (
        f"起動と後始末を 40 回繰り返した後、開いている fd が {before} から {after} に増えた")
    assert not [proc for proc in procs.STARTED if getattr(proc, "app_procs_number", -1) >= start], (
        "終わったものが、控えに残っている")


# ---------------------------------------------------------------------------------------------------------
# 4. 設計書 8.5 の、BROWSER にコマンドを指定する手順
# ---------------------------------------------------------------------------------------------------------

def manual_browser_commands() -> list[str]:
    """設計書の「手で確かめる項目」に書かれた、set BROWSER=… の値（コマンド プロンプトで打つもの）。"""
    text = DESIGN.read_text(encoding="utf-8")
    found = re.findall(r"`set BROWSER=([^`]+)`", text)
    assert found, "設計書に、BROWSER にコマンドを指定する手順（`set BROWSER=…`）が無い"
    return found


def test_the_manual_browser_command_writes_the_location_when_split_like_webbrowser(tmp_path):
    """手順のコマンドを、webbrowser と同じに分けて（shlex.split。%s に場所を入れる）起動すると、今いる
    フォルダの opened.txt に、渡された場所を書く（手順の「見ること」）。

    コマンドの中の python は、この試験を流している Python に置き換える（PATH に頼らない）。"""
    location = "file:///C:/Users/me/AppData/Local/circuit-sim/open-0123abcd.html"
    for value in manual_browser_commands():
        args = shlex.split(value)
        assert args[0] == "python" and args[-1] == "%s", args
        command = [sys.executable, *(arg.replace("%s", location) for arg in args[1:])]
        done = subprocess.run(command, cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=60, check=False)
        assert done.returncode == 0, (value, done.stderr)
        assert (tmp_path / "opened.txt").read_text(encoding="utf-8") == location
        (tmp_path / "opened.txt").unlink()


def test_the_manual_browser_command_reaches_webbrowser_whole_on_windows():
    """Windows の webbrowser は、BROWSER を os.pathsep（;）で区切って、いくつかの候補として読む
    （CPython 3.13 の Lib/webbrowser.py、register_standard_browsers）。手順のコマンドは、; で分かれない
    こと（後退を見張る試験。7回目は xfail(strict) だった。8aea404 で直り、8回目の検証で印を外した）。

    分かれると（7回目の値）、1つ目（python -c "import sys,pathlib）は起動に失敗し、2つ目は shlex.split が
    「No closing quotation」で失敗して、webbrowser.open が例外を出す。アプリ本体は「ブラウザを開けなかった」と
    案内し、opened.txt はできない。"""
    windows_pathsep = ";"
    for value in manual_browser_commands():
        parts = value.split(windows_pathsep)
        assert len(parts) == 1, f"Windows では、BROWSER が {len(parts)} つの候補に分かれる: {parts}"
