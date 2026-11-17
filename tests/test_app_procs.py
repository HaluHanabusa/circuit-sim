"""アプリの試験の部品（tests/app_procs.py）の試験: 起動したものを残さない。控えが、パイプを持ち続けない。

アプリ本体の代わりに、眠るだけの Python（引数に、試験ごとの印を持つ）を起動する。残ったかどうかは、
プロセスの一覧（ps）で印を探して見る。見つけたものは、試験の終わりに止める（印は試験ごとに違うので、
ほかの人のプロセスを止めない）。

本物の合図は、印で確かめた番号にだけ送る（D-078。レビュー 8回目の中1）:
- どの試験も、os.kill と os.killpg を gate（Gate）に替えて流す。部品の止め方（kill_tree）も、試験の
  終わりの後始末（reap）も、gate の中で動く。gate が本物を送るのは、この試験が起動して、まだ受け取って
  いない子と、印と「up」の行で確かめて許した番号だけ。ほかへの送信は控えて断り、試験を失敗にする。部品が
  後退しても、木の外に本物の合図は届かない
- 試験の終わりの印の後始末（kill_marked）は、送る直前に、その番号が印を持つことをもう一度確かめる
- 止め方の壊し方を確かめるとき（例外の位置など）は、合図を控えるだけの身代わりの木で流す（本物の合図を
  送らない）
"""
from __future__ import annotations

import errno
import os
import secrets
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import app_procs as procs
import pytest

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
TESTS = Path(__file__).resolve().parent
REAL_KILL = os.kill
REAL_KILLPG = getattr(os, "killpg", None)

# どのプロセスも、1 行を 1 回の書き込みで出す（PIPE_BUF 以下の書き込みは、ほかのプロセスの書き込みと
# 混ざらない）。print は、引数・区切り・改行を別々に書くので、出力をためない設定（PYTHONUNBUFFERED=1）では、
# 同じパイプに書く複数のプロセスの行が混ざる（レビュー 7回目の中2、検証 7回目の問題2）。

# 印を引数に持って、眠るだけのプロセス。起きたら「up 自分の番号」を出す
SLEEPER = ("import os, sys, time\nsys.stdout.write(f'up {os.getpid()}\\n')\nsys.stdout.flush()\n"
           "time.sleep(120)\n")

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
role = "spawner" if sys.argv[2:] == ["spawner"] else "parent"
sys.stdout.write(f"up {os.getpid()} {role}\\n")
sys.stdout.flush()
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
sys.stdout.write(f"ready {{plain.pid}} {{session.pid}}\\n")
sys.stdout.flush()
time.sleep(120)
"""


@pytest.fixture(autouse=True)
def no_process_left_behind(gate):
    """その試験が起動したプロセスが、試験の終わりに動いていたら、止めて（子も）、その試験を失敗にする。

    gate の中で動く（止める合図も、gate を通る）。"""
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


def stopped(token: str) -> list[str]:
    """引数に token を持つプロセスのうち、一時停止したまま（状態 T）のもの（POSIX。ps で探す）。"""
    done = subprocess.run(["ps", "-A", "-o", "pid=,stat=,args="], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        pytest.skip("ps が使えないので、残ったプロセスを確かめられない")
    found = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1].startswith(("T", "t")) and token in parts[2]:
            found.append(f"{parts[0]} {parts[1]}")
    return found


def tree_of(parent: procs.Launched) -> dict[str, int]:
    """PARENT の木の番号: parent、child（同じまとまりの子）、spawner（起動役）、grandchild（孫）。"""
    ups = [parent.wait_line("up", timeout=30).split() for _ in range(4)]
    roles = {words[2]: int(words[1]) for words in ups if len(words) == 3}
    sleepers = [int(words[1]) for words in ups if len(words) == 2]
    table = procs.children_table() or {}
    tree = {"parent": roles["parent"], "spawner": roles["spawner"]}
    tree["child"] = next(pid for pid in sleepers if pid in table.get(tree["parent"], []))
    tree["grandchild"] = next(pid for pid in sleepers if pid in table.get(tree["spawner"], []))
    assert tree["parent"] == parent.pid, (ups, tree)
    return tree


def gone_within(token: str, seconds: float) -> list[int]:
    """seconds 秒のうちに、印のプロセスが無くなれば []。残っていれば、その番号。"""
    deadline = time.monotonic() + seconds
    while True:
        left = marked(token)
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(0.1)


def has_mark(pid: int, token: str) -> bool:
    """pid のプロセスが、引数の1つとして token を持つか（POSIX。ゾンビと、無いものは偽）。

    Linux は /proc の引数、そのほかの OS は ps。"""
    if LINUX:
        try:
            return token.encode() in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
    done = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,args="], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30, check=False)
    line = done.stdout.strip()
    return done.returncode == 0 and bool(line) and not line.startswith("Z") and token in line.split()


def kill_marked(token: str) -> None:
    """引数に token を持つプロセスを止める（POSIX。印は試験ごとに違うので、ほかの人のプロセスを止めない）。

    一覧で見つけた番号は、送る直前に、印をもう一度確かめる（その間に終わって、番号が使い回されていたら
    送らない）。"""
    if not WINDOWS:
        for pid in marked(token):
            if has_mark(pid, token):
                try:
                    REAL_KILL(pid, signal.SIGKILL)
                except OSError:
                    pass


@pytest.fixture
def token():
    """試験ごとの印。試験の終わりに、印のプロセスが残っていれば止める（PC に残さない）。"""
    value = f"a0-procs-{secrets.token_hex(8)}"
    yield value
    kill_marked(value)


class Gate:
    """os.kill と os.killpg の差し替え（D-078。レビュー 8回目の中1）。

    本物の合図（send、send_group）を送るのは、次のものだけ:
    - この試験が spawn で起動し、まだ受け取っていない子（Popen が番号を押さえているので、使い回されない）
    - allow で許した番号のうち、送る直前にも、この試験の印（token）を引数に持つもの（印と「up」の行で
      確かめた子・孫、見張り）。印を持たなくなった番号（終わった、使い回された）には送らない
      （ProcessLookupError を出すだけ。断りとは数えない）
    - allow_group で許したまとまり（この試験が起動し、まだ受け取っていない、まとまりの先頭）
    ほかの番号とまとまりへの送信は、送らずに控え（refused）、ProcessLookupError（ESRCH）を出す。"""

    def __init__(self, token: str, send=None, send_group=None, check=None) -> None:
        self.token = token
        self.start = procs.mark()
        self.allowed: set[int] = set()
        self.groups: set[int] = set()
        self.sent: list[tuple] = []
        self.refused: list[tuple] = []
        self.send = send or REAL_KILL
        self.send_group = send_group or REAL_KILLPG
        self.check = check or has_mark  # 印の確かめ方（gate そのものの試験は、身代わりにする）

    def own_child(self, pid: int) -> bool:
        """この試験が spawn で起動し、まだ受け取っていない子か。"""
        return any(proc.pid == pid and proc.returncode is None
                   and getattr(proc, "app_procs_number", -1) >= self.start for proc in list(procs.STARTED))

    def kill(self, pid: int, sig: int) -> None:
        if self.own_child(pid) or (pid in self.allowed and self.check(pid, self.token)):
            self.sent.append((pid, int(sig)))
            return self.send(pid, sig)
        if pid in self.allowed:
            raise ProcessLookupError(errno.ESRCH, "gate: 許した番号だが、もう印を持たない（終わった）")
        self.refused.append((pid, int(sig)))
        raise ProcessLookupError(errno.ESRCH, "gate: 許す番号の外には送らない")

    def killpg(self, pgid: int, sig: int) -> None:
        if pgid in self.groups and self.own_child(pgid):
            self.sent.append(("group", pgid, int(sig)))
            return self.send_group(pgid, sig)
        self.refused.append(("group", pgid, int(sig)))
        raise ProcessLookupError(errno.ESRCH, "gate: 許すまとまりの外には送らない")

    def allow(self, *pids: int) -> None:
        """印を確かめてから、番号を許す（印が読めるまで、10 秒まで待つ）。"""
        for pid in pids:
            deadline = time.monotonic() + 10
            while not self.check(pid, self.token) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert self.check(pid, self.token), f"{pid} は、この試験の印を持たない（許さない）"
            self.allowed.add(pid)

    def allow_group(self, proc) -> None:
        """この試験が起動した、まだ動いているまとまりの先頭を許す。"""
        assert self.own_child(proc.pid) and proc.poll() is None and os.getpgid(proc.pid) == proc.pid
        self.groups.add(proc.pid)


@pytest.fixture(autouse=True)
def gate(token):
    """その試験の間（試験の終わりの後始末も）、os.kill と os.killpg を Gate に替える（POSIX）。

    終わりに、許していない所へ送ろうとしていたら、試験を失敗にする。"""
    if WINDOWS:
        yield None
        return
    current = Gate(token)
    os.kill, os.killpg = current.kill, current.killpg
    try:
        yield current
    finally:
        os.kill, os.killpg = REAL_KILL, REAL_KILLPG
    if current.refused:
        pytest.fail(f"許していない番号やまとまりに、合図を送ろうとした（gate が止めた）: {current.refused}",
                    pytrace=False)


only_posix = pytest.mark.skipif(WINDOWS, reason="POSIX のプロセスのまとまりの話（Windows は taskkill /T）")


@only_posix
def test_started_processes_die_when_the_harness_kills_the_check_with_its_group(tmp_path, token, gate):
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
        gate.allow_group(check.proc)
        os.killpg(check.pid, signal.SIGKILL)  # ハーネスの止め方（時間切れ、中断）
        check.wait(timeout=30)
        left = gone_within(token, 5)
        assert not left, f"確認をまとまりごと止めた後も、確認が起動したものが動いていた: {left}（{started}）"
    finally:
        check.stop()


@only_posix
def test_kill_tree_stops_children_and_grandchildren_while_the_parent_runs(tmp_path, token, gate):
    """kill_tree は、親が動いている間なら、子と孫も止める。自分だけのまとまりに移った子と、その孫も。

    前の止め方（起動したもののまとまりごと止める）では、自分だけのまとまりに移った子は残った。"""
    parent = procs.Launched([sys.executable, "-c", PARENT, token], cwd=tmp_path,
                            env={**os.environ, **PARENT_ENV})
    try:
        tree = tree_of(parent)  # 親、子、起動役、孫
        assert sorted(marked(token)) == sorted(tree.values()), (tree, marked(token))
        gate.allow(*tree.values())
        procs.kill_tree(parent.proc)
        assert {pid for pid, sig in gate.sent if sig == signal.SIGKILL} == set(tree.values()), gate.sent
        left = gone_within(token, 5)
        assert not left, f"親を止めた後も、子か孫が動いていた: {left}"
    finally:
        parent.stop()


class Interrupted(BaseException):
    """後始末の最中の Ctrl+C（KeyboardInterrupt）の身代わり（pytest そのものは止めない）。"""


@only_posix
@pytest.mark.parametrize("at", [1, 2], ids=["first-listing", "second-listing"])
def test_an_exception_while_tracing_the_tree_leaves_nothing_stopped(tmp_path, token, gate, monkeypatch, at):
    """子をたどる途中で例外が起きても、一時停止したものを止まったまま残さない（レビュー 7回目の中1）。

    at 回目の一覧（children_table）で例外を起こす。一時停止したものには、必ず SIGKILL を送る（finally）。
    たどれなかった子（例外の後で見つかるはずだったもの）は、止めない（動いたまま。印で、試験の終わりに止める）。
    - ふつうの起動の木（親、子、起動役、孫）
    - 新しいまとまりの起動（本体と見張り）。2 回目で例外なら、見張りも一時停止した後。止まったまま残ると、
      命綱が効かない"""
    tree = procs.Launched([sys.executable, "-c", PARENT, token], cwd=tmp_path,
                          env={**os.environ, **PARENT_ENV})
    alone = procs.Launched([sys.executable, "-c", SLEEPER, token], cwd=tmp_path, env=dict(os.environ),
                           new_session=True)
    try:
        members = tree_of(tree)
        alone.wait_line("up", timeout=30)
        deadline = time.monotonic() + 30
        while len(marked(token)) < 6 and time.monotonic() < deadline:
            time.sleep(0.05)  # 見張りが分かれるまで
        assert len(marked(token)) == 6, marked(token)
        guards = [pid for pid in marked(token) if pid not in {*members.values(), alone.pid}]
        assert len(guards) == 1, (guards, members)
        gate.allow(*members.values(), alone.pid, *guards)
        real_table = procs.children_table
        gated_kill = os.kill  # gate（許した番号にだけ本物を送る）
        stopped_by_it: list[int] = []
        calls = [0]

        def table():
            calls[0] += 1
            if calls[0] == at:
                raise Interrupted(f"{at} 回目の一覧で")
            return real_table()

        def kill(pid, sig):
            gated_kill(pid, sig)
            if sig == signal.SIGSTOP:
                stopped_by_it.append(pid)

        monkeypatch.setattr(procs, "children_table", table)
        monkeypatch.setattr(procs.os, "kill", kill)
        for launched in (tree, alone):
            calls[0] = 0
            with pytest.raises(Interrupted):
                procs.kill_tree(launched.proc)
        monkeypatch.undo()
        assert tree.pid in stopped_by_it and alone.pid in stopped_by_it
        assert not gate.refused, ("許していない番号に送ろうとした", gate.refused)
        deadline = time.monotonic() + 5
        while (left := stopped(token)) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not left, f"一時停止したまま残った（番号、状態）: {left}"
        alive = set(marked(token))
        assert not alive & set(stopped_by_it), f"一時停止したのに、止めていない: {alive & set(stopped_by_it)}"
    finally:
        monkeypatch.undo()
        kill_marked(token)  # たどれなかった子（出力のパイプを持っている）を先に止める
        tree.stop()
        alone.stop()


@only_posix
def test_kill_tree_stops_the_root_before_listing_and_kills_children_first(tmp_path, token, gate, monkeypatch):
    """番号を使い回されたほかの人のプロセスを止めないための順序（kill_posix_tree の説明の 1〜3）:

    - 最初に root を一時停止し、その後で一覧を作る
    - 一時停止するのは、それより前に一時停止した親の子として、一覧に載っていたものだけ
    - SIGKILL は、一時停止がすべて済んでから、子から先に送る
    - 一時停止が失敗した番号（もう無い）には、SIGKILL を送らない
    os.kill と一覧（children_table）を見張って、順を控える。孫の一時停止は「もう無い」と答えさせる（本物の
    孫は動いたまま。試験の終わりに印で止める）。"""
    parent = procs.Launched([sys.executable, "-c", PARENT, token], cwd=tmp_path,
                            env={**os.environ, **PARENT_ENV})
    try:
        tree = tree_of(parent)
        gate.allow(*tree.values())
        events: list[tuple] = []
        real_table = procs.children_table
        gated_kill = os.kill  # gate（許した番号にだけ本物を送る）

        def table():
            result = real_table()
            events.append(("table", {p: list(c) for p, c in (result or {}).items()}))
            return result

        def kill(pid, sig):
            if sig == signal.SIGSTOP and pid == tree["grandchild"]:
                events.append(("stop-failed", pid))
                raise ProcessLookupError(errno.ESRCH, "No such process")
            gated_kill(pid, sig)
            events.append((sig, pid))

        monkeypatch.setattr(procs, "children_table", table)
        monkeypatch.setattr(procs.os, "kill", kill)
        procs.kill_posix_tree(parent.pid)
        monkeypatch.undo()
        assert not gate.refused, ("許していない番号に送ろうとした", gate.refused)
        stops = [i for i, e in enumerate(events) if e[0] == signal.SIGSTOP]
        kills = [i for i, e in enumerate(events) if e[0] == signal.SIGKILL]
        tables = [i for i, e in enumerate(events) if e[0] == "table"]
        assert events[0] == (signal.SIGSTOP, parent.pid), ("最初に root を一時停止していない", events)
        assert tables and tables[0] > 0, ("root を一時停止する前に、一覧を作った", events)
        for i in stops[1:]:
            pid = events[i][1]
            listed = any(pid in events[j][1].get(events[k][1], [])
                         for j in tables if j < i for k in stops if k < j)
            assert listed, (f"{pid} は、一時停止した親の子として一覧に載る前に、一時停止された", events)
        stopped_pids = {events[i][1] for i in stops}
        assert stopped_pids == {tree["parent"], tree["child"], tree["spawner"]}, (tree, events)
        assert kills and min(kills) > max(stops), ("一時停止が済む前に、SIGKILL を送った", events)
        killed = [events[i][1] for i in kills]
        assert set(killed) == stopped_pids, ("一時停止したものだけに SIGKILL を送っていない", events)
        assert tree["grandchild"] not in killed, "一時停止が失敗した番号に、SIGKILL を送った"
        parents = {child: p for e in events if e[0] == "table" for p, cs in e[1].items() for child in cs}
        for pid in killed:
            if parents.get(pid) in killed:
                assert killed.index(pid) < killed.index(parents[pid]), ("子より先に親を止めた", killed, tree)
    finally:
        monkeypatch.undo()
        kill_marked(token)  # 一時停止が「もう無い」と答えさせた孫（出力のパイプを持っている）を先に止める
        parent.stop()


# 合図を控えるだけの身代わりの木（本物の合図は送らない。D-078）。番号は、どの OS でも動いているプロセスに
# 当たらない値（Linux の番号の上限 4194304、macOS の 99998 より大きい）
FAKE_ROOT, FAKE_A, FAKE_B, FAKE_C = 9_000_001, 9_000_002, 9_000_003, 9_000_004
FAKE_TREE = {FAKE_ROOT: [FAKE_A, FAKE_B], FAKE_B: [FAKE_C]}


class FakeTree:
    """children_table と os.kill の身代わり。合図は控えて状態を変えるだけで、本当には送らない。

    interrupt=(kind, n): "stop" は n 回目の SIGSTOP を届けた後、"kill" は n 回目の SIGKILL を届けた後、
    "before-stop" は n 回目の SIGSTOP を届ける前に、Interrupted を出す（CPython の os.kill は、送った後に
    待っている合図を処理するので、Ctrl+C が os.kill の中で KeyboardInterrupt になる。検証 8回目に実測）。
    gone は、一覧に載ったが、一時停止の前に終わって受け取られていた番号（ESRCH を返す）。"""

    def __init__(self, interrupt: tuple[str, int] | None = None, gone: frozenset[int] = frozenset()) -> None:
        self.state = {pid: "S" for pid in (FAKE_ROOT, FAKE_A, FAKE_B, FAKE_C)}
        self.interrupt = interrupt
        self.gone = gone
        self.events: list[tuple] = []
        self.counts = {"stop": 0, "kill": 0}

    def table(self) -> dict[int, list[int]]:
        return {parent: list(children) for parent, children in FAKE_TREE.items()}

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.state:
            self.events.append(("outside", pid, int(sig)))
            raise ProcessLookupError(errno.ESRCH, "木の外（身代わりは送らない）")
        kind = {signal.SIGSTOP: "stop", signal.SIGKILL: "kill"}.get(sig, "other")
        if kind in self.counts:
            self.counts[kind] += 1
        if kind == "stop" and self.interrupt == ("before-stop", self.counts["stop"]):
            raise Interrupted(f"{self.counts['stop']} 回目の SIGSTOP の前")
        if pid in self.gone or self.state[pid] == "dead":
            self.events.append(("missing", pid, int(sig)))
            raise ProcessLookupError(errno.ESRCH, "No such process")
        self.events.append((int(sig), pid))
        if kind == "stop":
            self.state[pid] = "T"
        elif kind == "kill":
            self.state[pid] = "dead"
        if self.interrupt == (kind, self.counts.get(kind)):
            raise Interrupted(f"{self.counts[kind]} 回目の {kind} の後")

    def killpg(self, pgid: int, sig: int) -> None:
        self.events.append(("outside-group", pgid, int(sig)))
        raise ProcessLookupError(errno.ESRCH, "まとまりには送らない（身代わり）")


@only_posix
@pytest.mark.parametrize("where", [
    *[("before-stop", n) for n in range(1, 5)],
    *[("stop", n) for n in range(1, 5)],
    *[("kill", n) for n in range(1, 5)],
    None,
], ids=lambda where: "no-interrupt" if where is None else f"{where[0]}-{where[1]}")
def test_an_interrupt_from_inside_os_kill_leaves_nothing_stopped(monkeypatch, where):
    """os.kill の中から例外が出ても（送る前でも、送った後でも）、一時停止したものを止まったまま残さない。
    例外は、全部に SIGKILL を送ってから出し直す（検証 8回目の問題1、レビュー 8回目の軽微1）。

    身代わりの木（root、子 A・B、B の子 C）で流す。合図は控えるだけで、本当には送らない（D-078）。
    - 一時停止を届けたもの（状態 T）は、どれも、その後で SIGKILL を受ける
    - 一時停止を送る前に例外が出た番号も、控えているので SIGKILL を受ける（一覧に載った、止めた親の子）
    - 木の外にも、まとまりにも、送らない"""
    tree = FakeTree(interrupt=where)
    monkeypatch.setattr(procs, "os", types.SimpleNamespace(kill=tree.kill, killpg=tree.killpg))
    monkeypatch.setattr(procs, "children_table", tree.table)
    try:
        procs.kill_posix_tree(FAKE_ROOT)
        raised = None
    except Interrupted as exc:
        raised = str(exc)
    monkeypatch.undo()
    assert (raised is None) == (where is None), (raised, tree.events)
    left = [pid for pid, state in tree.state.items() if state == "T"]
    assert not left, (f"一時停止したまま残った: {left}", tree.events)
    stopped_ = [e[1] for e in tree.events if e[0] == signal.SIGSTOP]
    killed = [e[1] for e in tree.events if e[0] == signal.SIGKILL]
    assert set(stopped_) <= set(killed), ("一時停止したのに、SIGKILL を受けていないものがある", tree.events)
    assert not [e for e in tree.events if str(e[0]).startswith("outside")], tree.events
    if where is None:
        assert killed == [FAKE_C, FAKE_B, FAKE_A, FAKE_ROOT], ("子から先に止めていない", tree.events)


@only_posix
def test_a_pid_that_could_not_be_stopped_gets_no_sigkill(monkeypatch):
    """一覧に載ったが、一時停止を送ったら「もう無い」（ESRCH）だった番号には、SIGKILL を送らない（その番号は、
    もう別のプロセスのものかもしれない）。身代わりの木で流す（合図は送らない）。"""
    tree = FakeTree(gone=frozenset({FAKE_C}))
    monkeypatch.setattr(procs, "os", types.SimpleNamespace(kill=tree.kill, killpg=tree.killpg))
    monkeypatch.setattr(procs, "children_table", tree.table)
    procs.kill_posix_tree(FAKE_ROOT)
    monkeypatch.undo()
    killed = [e[1] for e in tree.events if e[0] == signal.SIGKILL]
    assert killed == [FAKE_B, FAKE_A, FAKE_ROOT], tree.events
    assert [e for e in tree.events if e[0] == "missing"] == [("missing", FAKE_C, int(signal.SIGSTOP))]


@only_posix
def test_the_gate_sends_only_to_its_own_children_and_marked_members():
    """gate（Gate）そのものの確かめ。送り先と印の確かめを身代わりにして流す（本物の合図は送らない）。

    - この試験が起動し、まだ受け取っていない子には送る。受け取った後は送らない（番号が使い回されうる）
    - 許していない番号には送らず、控えて断る（ProcessLookupError）
    - 許した番号でも、送る直前に印を持たなければ送らない（終わった、使い回された）。断りとは数えない
    - 許していないまとまりには送らない
    控えの「受け取っていない子」は、存在しえない番号の身代わり（Popen の代わり）を控えに入れて作る
    （後始末の前に外す）。"""
    sent: list[tuple] = []
    marks: dict[int, bool] = {}
    local = Gate("unused", send=lambda pid, sig: sent.append((pid, int(sig))),
                 send_group=lambda pgid, sig: sent.append(("group", pgid, int(sig))),
                 check=lambda pid, token: marks.get(pid, False))
    own = types.SimpleNamespace(pid=9_100_001, returncode=None, app_procs_number=procs.mark(), stdin=None)
    member, stranger = 9_100_002, 9_100_003
    procs.STARTED.append(own)
    try:
        local.kill(own.pid, 0)
        with pytest.raises(ProcessLookupError):
            local.kill(member, signal.SIGKILL)  # まだ許していない
        marks[member] = True
        local.allow(member)
        local.kill(member, signal.SIGKILL)
        marks[member] = False  # 終わった（印を持たなくなった）
        with pytest.raises(ProcessLookupError):
            local.kill(member, signal.SIGKILL)
        with pytest.raises(ProcessLookupError):
            local.kill(stranger, signal.SIGSTOP)
        with pytest.raises(ProcessLookupError):
            local.killpg(os.getpgid(0), signal.SIGKILL)
        own.returncode = 0  # 受け取った
        with pytest.raises(ProcessLookupError):
            local.kill(own.pid, signal.SIGKILL)
    finally:
        procs.STARTED.remove(own)
    assert sent == [(own.pid, 0), (member, int(signal.SIGKILL))], sent
    assert local.refused == [(member, int(signal.SIGKILL)), (stranger, int(signal.SIGSTOP)),
                             ("group", os.getpgid(0), int(signal.SIGKILL)), (own.pid, int(signal.SIGKILL))]


@only_posix
def test_the_guard_watches_a_lifeline_with_a_large_fd_number(tmp_path, token, monkeypatch):
    """命綱の fd が 1024 以上でも、見張りは命綱を見続け、切れたら止める（レビュー 7回目の軽微1）。

    select.select は 1024 以上の fd で ValueError になる（前の見張りは、そこで黙って終わっていた）。
    spawn が作る命綱のパイプを、大きな番号に移して起動し、命綱を閉じる（試験のプロセスが終わったときと
    同じ）。"""
    import resource

    high = 1500
    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= high + 10:
        pytest.skip("開ける fd の上限が小さい")
    real_pipe = os.pipe
    calls = [0]

    def pipe():
        calls[0] += 1
        r, w = real_pipe()
        if calls[0] > 1:
            return r, w  # 命綱のほか（Popen が使うパイプ）はそのまま
        number = high
        while True:
            try:
                os.fstat(number)
                number += 1  # 使われている
            except OSError:
                break
        os.dup2(r, number, inheritable=False)
        os.close(r)
        return number, w

    monkeypatch.setattr(procs.os, "pipe", pipe)
    proc = procs.Launched([sys.executable, "-c", SLEEPER, token], cwd=tmp_path, env=dict(os.environ),
                          new_session=True)
    monkeypatch.undo()
    try:
        proc.wait_line("up", timeout=30)
        assert calls[0] >= 1
        deadline = time.monotonic() + 30
        while len(marked(token)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)  # 見張りが分かれるまで
        time.sleep(0.5)  # 見張りが命綱を見始めるまで（前の見張りは、ここで黙って終わっていた）
        assert len(marked(token)) == 2, ("見張りが動いていない", marked(token))
        procs.close_lifeline(proc.proc)  # 試験のプロセスが終わったときと同じ（命綱が切れる）
        assert proc.wait(timeout=10) == -signal.SIGKILL, "命綱が切れても、見張りが止めない"
        assert not gone_within(token, 5)
    finally:
        monkeypatch.undo()
        proc.stop()


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
def test_terminal_signals_do_not_reach_the_guard(tmp_path, token, gate):
    """見張りは、自分だけのまとまりにいる（本来のコマンドのまとまりに送られる端末の合図を受けない）。"""
    proc = procs.Launched([sys.executable, "-c", SLEEPER, token], cwd=tmp_path, env=dict(os.environ),
                          new_session=True)
    try:
        proc.wait_line("up", timeout=30)
        guards = [pid for pid in marked(token) if pid != proc.pid]
        assert len(guards) == 1, marked(token)
        assert os.getpgid(guards[0]) != os.getpgid(proc.pid)
        gate.allow_group(proc.proc)
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
