"""A0 の独立検証（verifier、8回目）: 7回目の中1（止める途中の例外）と中3（設計書 8.5 の BROWSER の手順）の
直し（eb2a218、8aea404）の確かめ。

記録は docs/verification/A0-verifier-8.md。

1. 設計書 8.5 の BROWSER の値を、この OS の本物の webbrowser に渡す（Windows では、Windows の本物の道を通る:
   BROWSER を ; で区切る、引数の並びを 1 本のコマンドラインにして起動する）。最初の候補が値の全部で、起動
   されたコマンドが、今いるフォルダの opened.txt に場所を書く。コマンドの python は、この試験を流している
   Python に置き換える（PATH に頼らない。手順のとおり / で区切った場所）。最初の候補だけを開く（ほかの候補、
   たとえば既定のブラウザを開かせない）。
2. Windows でない OS では、Windows の webbrowser の読み方をまねて確かめる（別の Python で、sys.platform を
   win32、os.pathsep を ; にしてから webbrowser を読み込む。起動は控えるだけで、引数の並びは
   subprocess.list2cmdline で 1 本にしてから、Microsoft の C ランタイムの決まりで分け直す）。まねが効いて
   いることは、; を含む値（7回目の手順）が 2 つに分かれることで確かめる。
3. tests/app_procs.py の kill_posix_tree に、本物の合図を送らずに例外を差し込む。存在しえない番号
   （9000001 から。Linux の番号の上限 4194304、macOS の 99999 より大きい）の木を一覧の身代わりで渡し、
   os.kill は控えるだけの身代わりにする（D-078: 合図を送る道を壊す確かめでは、本物の合図を送らない）。
   - 一覧の途中の例外: 一時停止したものを、止まったまま残さない（7回目の中1。直った）
   - 止める順: root を先に一時停止する、一覧に載った子だけを一時停止する、SIGKILL は一時停止が済んで
     から子から先に、一時停止したものだけに送る、木の外に送らない
   - 軽微（xfail(strict)。8回目の問題1）: os.kill が合図を届けた後に出す例外。CPython の os.kill は、合図を
     送った後に、待っている合図を処理する（Python 3.13.18 で確かめた: Ctrl+C を立ててから、自分の子に
     os.kill(子, SIGSTOP) を呼ぶと、子は一時停止し、os.kill は KeyboardInterrupt を出す）。だから、後始末の
     最中の Ctrl+C は、os.kill の中から出うる。一時停止を届けた直後に出ると、その番号は「一時停止したもの」に
     控えられず、止まったまま残る。SIGKILL を送っている途中に出ると（最初の例外でも）、残りに SIGKILL が
     送られない（部品の説明は「さらに例外（3 回目の Ctrl+C など）」と書き、最初の例外でも起こることを
     書いていない）。
4. 受け入れ試験の後始末の、印の確かめ（a0_marked。レビュー 8回目の中1・軽微3 に合わせて、番号から止める
   後始末を、印で確かめてから送る形にそろえた）が、印を持つものだけを自分のものと見る。合図は送らない。
"""
from __future__ import annotations

import json
import os
import re
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
DESIGN = ROOT / "docs" / "design" / "app-design.md"
WINDOWS = os.name == "nt"
LOCATION = "file:///C:/Users/me/AppData/Local/circuit-sim/open-0123abcd.html"


def manual_browser_values() -> list[str]:
    """設計書の「手で確かめる項目」に書かれた、set BROWSER=… の値（コマンド プロンプトで打つもの）。"""
    found = re.findall(r"`set BROWSER=([^`]+)`", DESIGN.read_text(encoding="utf-8"))
    assert found, "設計書に、BROWSER にコマンドを指定する手順（`set BROWSER=…`）が無い"
    return found


def child_env(browser: str) -> dict[str, str]:
    """子の Python の環境変数。BROWSER を value にし、画面と端末の手掛かりを外す（webbrowser が、ほかの
    ブラウザを探すために、別のコマンドを起動しないように）。"""
    env = {**os.environ, "BROWSER": browser}
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "TERM"):
        env.pop(name, None)
    return env


# ---------------------------------------------------------------------------------------------------------
# 1. この OS の本物の webbrowser に、8.5 の値を渡す
# ---------------------------------------------------------------------------------------------------------

# 最初の候補だけを開く（webbrowser.open は、開けなければ次の候補（既定のブラウザなど）へ進むので使わない）
OPEN_FIRST = r"""
import json, os, sys, webbrowser
if webbrowser._tryorder is None:
    webbrowser.register_standard_browsers()
order = list(webbrowser._tryorder)
try:
    opened, error = webbrowser.get(order[0]).open(sys.argv[1]), None
except Exception as exc:
    opened, error = None, f"{type(exc).__name__}: {exc}"
print(json.dumps({"order": order, "opened": opened, "error": error, "pathsep": os.pathsep}))
"""


def with_this_python(value: str) -> str:
    """値の先頭の python を、この試験を流している Python の場所（/ で区切り、引用符で囲む）に置き換える。"""
    first, rest = value.split(" ", 1)
    assert first == "python", value
    return '"' + sys.executable.replace("\\", "/") + '" ' + rest


def test_the_manual_browser_value_reaches_this_os_webbrowser_whole_and_writes_the_location(tmp_path):
    """8.5 の値を BROWSER にして、この OS の webbrowser に読ませる。

    - 最初の候補が、値の全部（Windows では ; で区切られる。値が分かれていない）
    - その候補で開くと、起動されたコマンドが、今いるフォルダの opened.txt に、渡された場所を書く
    Windows では、BROWSER の区切り（;）、引数の並びを 1 本のコマンドラインにする所、python の引数の分け方を、
    本物で通る（cmd.exe の set の扱いは通らない。手で確かめる項目）。"""
    for value in manual_browser_values():
        browser = with_this_python(value)
        done = subprocess.run([sys.executable, "-I", "-c", OPEN_FIRST, LOCATION], cwd=tmp_path,
                              env=child_env(browser), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        assert done.returncode == 0, (value, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        assert result["order"][:1] == [browser], (
            f"BROWSER が、区切り {result['pathsep']!r} で分かれた（最初の候補が値の全部でない）", result)
        assert result["opened"] is True, ("最初の候補で開けなかった", result, done.stderr)
        assert (tmp_path / "opened.txt").read_text(encoding="utf-8") == LOCATION, result
        (tmp_path / "opened.txt").unlink()


# ---------------------------------------------------------------------------------------------------------
# 2. Windows の webbrowser の読み方を、Windows でない OS でまねる
# ---------------------------------------------------------------------------------------------------------

# 引数: BROWSER の値、場所。結果を JSON で出す。起動は控えるだけ（本当には起動しない）
AS_ON_WINDOWS = r'''
import json, os, shlex, shutil, subprocess, sys, threading  # 先に読み込む（Windows の道で読み込ませない）

def ms_split(line):
    """Microsoft の C ランタイムの決まり（2008 年以降）で、コマンドラインを引数に分ける。"""
    args, cur, i, quoted, have, n = [], [], 0, False, False, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            j = i
            while j < n and line[j] == "\\":
                j += 1
            if j < n and line[j] == '"':
                cur.append("\\" * ((j - i) // 2))
                if (j - i) % 2:
                    cur.append('"')
                    j += 1
                i, have = j, True
                continue
            cur.append("\\" * (j - i))
            i, have = j, True
            continue
        if c == '"':
            if quoted and i + 1 < n and line[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            quoted, have, i = not quoted, True, i + 1
            continue
        if c in " \t" and not quoted:
            if have:
                args.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        cur.append(c)
        have, i = True, i + 1
    if have:
        args.append("".join(cur))
    return args

started, startfile = [], []

class Recorder:
    def __init__(self, cmd, *args, **kwargs):
        started.append(ms_split(subprocess.list2cmdline(cmd)))
    def wait(self, timeout=None):
        return 0

value, location = sys.argv[1], sys.argv[2]
sys.platform = "win32"
os.pathsep = ";"
os.environ["BROWSER"] = value
shutil.which = lambda name, *args, **kwargs: "C:\\Python313\\python.exe" if name == "python" else None
os.startfile = lambda *args, **kwargs: startfile.append(args)
subprocess.Popen = Recorder
import webbrowser
webbrowser.register_standard_browsers()
order = list(webbrowser._tryorder)
try:
    opened, error = webbrowser.open(location), None
except Exception as exc:
    opened, error = None, f"{type(exc).__name__}: {exc}"
print(json.dumps({"windows_default": hasattr(webbrowser, "WindowsDefault"), "order": order,
                  "opened": opened, "error": error, "started": started, "startfile": len(startfile)}))
'''


def as_on_windows(value: str) -> dict:
    done = subprocess.run([sys.executable, "-I", "-c", AS_ON_WINDOWS, value, LOCATION],
                          env=child_env(value), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, check=False)
    assert done.returncode == 0, (value, done.stderr)
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["windows_default"], ("Windows の道で読み込めていない（まねが効いていない）", result)
    return result


@pytest.mark.skipif(WINDOWS, reason="Windows では 1 が本物の webbrowser で確かめる")
def test_the_manual_browser_value_survives_the_windows_reading_of_webbrowser():
    """Windows の webbrowser の読み方（; で区切る、1 本のコマンドラインにして起動する）をまねて、8.5 の値が
    1 つの候補のまま、['python', '-c', <コード>, <場所>] で起動されることを確かめる。既定のブラウザ
    （os.startfile）には進まない。

    まねが効いていること: 7回目の手順の値（Python の文を ; で区切っていた）は、同じまねで 2 つに分かれる。"""
    old = "python -c \"import sys,pathlib;pathlib.Path('opened.txt').write_text(sys.argv[1])\" %s"
    control = as_on_windows(old)
    assert old not in control["order"] and len([c for c in control["order"] if "pathlib" in c]) == 2, control
    for value in manual_browser_values():
        result = as_on_windows(value)
        assert result["order"][:1] == [value], ("BROWSER が ; で分かれた", result)
        expected = [arg.replace("%s", LOCATION) for arg in shlex.split(value)]
        assert result["started"] == [expected], ("Windows で起動される引数が違う", result)
        assert result["opened"] is True and result["startfile"] == 0, result


# ---------------------------------------------------------------------------------------------------------
# 3. kill_posix_tree に、本物の合図を送らずに例外を差し込む
# ---------------------------------------------------------------------------------------------------------

only_posix = pytest.mark.skipif(WINDOWS, reason="POSIX の止め方（SIGSTOP と SIGKILL）の話")

# 存在しえない番号の木: root の子が A、B。B の子が C
FAKE_ROOT, FAKE_A, FAKE_B, FAKE_C = 9_000_001, 9_000_002, 9_000_003, 9_000_004
FAKE_TREE = {FAKE_ROOT: [FAKE_A, FAKE_B], FAKE_B: [FAKE_C]}


class Interrupted(BaseException):
    """後始末の最中の Ctrl+C（KeyboardInterrupt）の身代わり（pytest そのものは止めない）。"""


class FakeTree:
    """一覧（children_table）と os.kill の身代わり。合図は控えるだけで、本当には送らない。

    interrupt: ("table", n) は n 回目の一覧で、("stop", n) は n 回目の SIGSTOP を届けた後に、
    ("kill", n) は n 回目の SIGKILL を届けた後に、Interrupted を出す。gone は、一覧に載った後、一時停止の
    前に終わって受け取られていた番号（一覧には載るが、合図は ESRCH）。"""

    def __init__(self, interrupt: tuple[str, int] | None = None, gone: frozenset[int] = frozenset()) -> None:
        self.state = {pid: "S" for pid in (FAKE_ROOT, FAKE_A, FAKE_B, FAKE_C)}
        self.interrupt = interrupt
        self.gone = gone
        self.events: list[tuple] = []
        self.counts = {"table": 0, "stop": 0, "kill": 0}

    def _maybe_interrupt(self, kind: str) -> None:
        self.counts[kind] += 1
        if self.interrupt == (kind, self.counts[kind]):
            raise Interrupted(f"{self.counts[kind]} 回目の {kind}")

    def table(self) -> dict[int, list[int]]:
        self.events.append(("table",))
        self._maybe_interrupt("table")
        return {parent: [c for c in children if self.state[c] != "dead"]
                for parent, children in FAKE_TREE.items() if self.state[parent] != "dead"}

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.state:
            self.events.append(("outside", pid, sig))
            raise ProcessLookupError(3, "木の外（身代わりは送らない）")
        if pid in self.gone or self.state[pid] == "dead":
            self.events.append(("missing", pid, sig))
            raise ProcessLookupError(3, "No such process")
        self.events.append((sig, pid))
        if sig == signal.SIGSTOP:
            self.state[pid] = "T"
            self._maybe_interrupt("stop")
        elif sig == signal.SIGKILL:
            self.state[pid] = "dead"
            self._maybe_interrupt("kill")

    def killpg(self, pgid: int, sig: int) -> None:
        self.events.append(("outside-group", pgid, sig))
        raise ProcessLookupError(3, "まとまりには送らない（身代わり）")

    def left_stopped(self) -> list[int]:
        return [pid for pid, state in self.state.items() if state == "T"]


class FakeOs:
    """app_procs の os の身代わり: kill と killpg だけを FakeTree に替え、ほかは本物の os。"""

    def __init__(self, tree: FakeTree) -> None:
        self.kill = tree.kill
        self.killpg = tree.killpg

    def __getattr__(self, name: str):
        return getattr(os, name)


def run_kill_posix_tree(monkeypatch, tree: FakeTree) -> str | None:
    """kill_posix_tree(FAKE_ROOT) を身代わりの中で流す。出た Interrupted の説明（出なければ None）を返す。"""
    monkeypatch.setattr(procs, "os", FakeOs(tree))
    monkeypatch.setattr(procs, "children_table", tree.table)
    try:
        procs.kill_posix_tree(FAKE_ROOT)
        raised = None
    except Interrupted as exc:
        raised = str(exc)
    finally:
        monkeypatch.undo()
    outside = [e for e in tree.events if e[0] in ("outside", "outside-group")]
    assert not outside, ("木の外に送った", tree.events)
    return raised


@only_posix
@pytest.mark.parametrize("listing", [1, 2, 3])
def test_an_exception_from_the_listing_leaves_nothing_stopped(monkeypatch, listing):
    """listing 回目の一覧で例外が出ても、一時停止したものは、どれも SIGKILL で止める（7回目の中1 の直し）。"""
    tree = FakeTree(interrupt=("table", listing))
    raised = run_kill_posix_tree(monkeypatch, tree)
    assert raised == f"{listing} 回目の table", (raised, tree.events)
    assert not tree.left_stopped(), (f"一時停止したまま残った: {tree.left_stopped()}", tree.events)


@only_posix
@pytest.mark.parametrize("gone", [frozenset(), frozenset({FAKE_C})], ids=["all-alive", "grandchild-gone"])
def test_kill_posix_tree_stops_first_and_kills_only_what_it_stopped_children_first(monkeypatch, gone):
    """止める順（kill_posix_tree の説明の 1〜3）を、本物の合図を送らずに確かめる。

    - 最初に root を一時停止し、その後で一覧を作る
    - 一時停止するのは、それより前に一時停止した親の子として、一覧に載っていたものだけ
    - SIGKILL は、一時停止がすべて済んでから、一時停止したものだけに、子から先に送る
    - 一時停止が失敗した番号（もう無い）には、SIGKILL を送らない。木の外には何も送らない"""
    tree = FakeTree(gone=gone)
    assert run_kill_posix_tree(monkeypatch, tree) is None
    events = tree.events
    assert events[0] == (signal.SIGSTOP, FAKE_ROOT), events
    assert events[1] == ("table",), ("root を一時停止した後に、一覧を作っていない", events)
    stops = [i for i, e in enumerate(events) if e[0] == signal.SIGSTOP]
    kills = [i for i, e in enumerate(events) if e[0] == signal.SIGKILL]
    stopped = [events[i][1] for i in stops]
    for i in stops[1:]:
        pid = events[i][1]
        parent = next(p for p, children in FAKE_TREE.items() if pid in children)
        assert any(events[j] == (signal.SIGSTOP, parent) for j in range(i)), (pid, events)
        assert any(events[j] == ("table",) for j in range(i) if j > events.index((signal.SIGSTOP, parent))), (
            pid, events)
    assert kills and min(kills) > max(stops), ("一時停止が済む前に SIGKILL を送った", events)
    killed = [events[i][1] for i in kills]
    assert sorted(killed) == sorted(stopped), ("一時停止したものだけに SIGKILL を送っていない", events)
    tried_on_gone = [e for e in events if e[0] == "missing" and e[2] == signal.SIGKILL]
    assert not tried_on_gone, ("一時停止が失敗した番号に、SIGKILL を送ろうとした", events)
    if gone:
        assert ("missing", FAKE_C, signal.SIGSTOP) in events, (
            "終わっていた孫を、一時停止しようとしていない", events)
    for pid in killed:
        parent = next((p for p, children in FAKE_TREE.items() if pid in children), None)
        if parent in killed:
            assert killed.index(pid) < killed.index(parent), ("子より先に親を止めた", killed)
    assert not tree.left_stopped(), tree.events


SURFACING = pytest.mark.xfail(strict=True, reason=(
    "軽微（A0 の検証 8回目の問題1）: os.kill が合図を届けた後に出す例外（CPython の os.kill は、送った後に"
    "待っている合図を処理する）。一時停止を届けた直後なら、その番号は控えられずに止まったまま残る。SIGKILL を"
    "送っている途中なら、最初の例外でも、残りに SIGKILL が送られない"
))


@only_posix
@pytest.mark.parametrize("where", [
    pytest.param(("stop", 1), id="after-stopping-root", marks=SURFACING),
    pytest.param(("stop", 2), id="after-stopping-child-a", marks=SURFACING),
    pytest.param(("stop", 3), id="after-stopping-child-b", marks=SURFACING),
    pytest.param(("stop", 4), id="after-stopping-grandchild", marks=SURFACING),
    pytest.param(("kill", 1), id="after-the-first-sigkill", marks=SURFACING),
    pytest.param(("kill", 2), id="after-the-second-sigkill", marks=SURFACING),
    pytest.param(("kill", 3), id="after-the-third-sigkill", marks=SURFACING),
    pytest.param(("kill", 4), id="after-the-last-sigkill"),
])
def test_an_interrupt_surfacing_from_os_kill_leaves_nothing_stopped(monkeypatch, where):
    """os.kill が合図を届けた後に例外が出ても（後始末の最中の Ctrl+C が、os.kill の中で KeyboardInterrupt に
    なる形）、一時停止したものを、止まったまま残さない。

    最後の SIGKILL の後（after-the-last-sigkill）は、いまでも残らない（比べのため）。"""
    tree = FakeTree(interrupt=where)
    raised = run_kill_posix_tree(monkeypatch, tree)
    assert raised == f"{where[1]} 回目の {where[0]}", (raised, tree.events)
    assert not tree.left_stopped(), (f"一時停止したまま残った: {tree.left_stopped()}", tree.events)


# ---------------------------------------------------------------------------------------------------------
# 4. 後始末の印の確かめ（a0_marked）
# ---------------------------------------------------------------------------------------------------------

def test_the_mark_check_accepts_only_processes_that_carry_the_marks(tmp_path):
    """a0_marked.is_marked は、与えた印（Linux は環境変数と引数、ほかの OS は引数）をどれも持つプロセスだけを
    自分のものと見る。身代わりのブラウザと同じ形（場所を / で区切った引数）で起動して確かめる。合図は送らない
    （確かめるだけ。後始末は Popen の kill で、受け取る前の自分の子にだけ）。"""
    if not marked.LINUX and watch.list_processes() is None:
        pytest.skip("この PC では、プロセスの一覧を作れない（引数の印を確かめられない）")
    script = tmp_path / "slow_browser.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    entry = f"A0V8_OUT={tmp_path}"
    needle = f"{tmp_path.name}/slow_browser.py"
    proc = subprocess.Popen([sys.executable, str(script).replace("\\", "/"), "file:///x.html"],
                            env={**os.environ, "A0V8_OUT": str(tmp_path)}, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        environ = entry if marked.LINUX else None
        # 起動の直後は、まだ実行ファイルの入れ替えの途中で、引数と環境変数が空に読めることがある（Linux で
        # 実測。そのときは自分のものと見ない。安全側）。読めるようになるまで待つ（上限 10 秒）
        deadline = time.monotonic() + 10
        while not marked.is_marked(proc.pid, environ=environ, command=needle) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marked.is_marked(proc.pid, environ=environ, command=needle), "印を持つのに、自分のものと見ない"
        assert not marked.is_marked(proc.pid, environ=environ, command=f"{tmp_path.name}/other.py")
        assert not marked.is_marked(proc.pid), "印を与えないのに、自分のものと見た"
        if marked.LINUX:
            assert not marked.is_marked(proc.pid, environ=entry + "-other", command=needle)
        else:
            assert not marked.is_marked(proc.pid, environ=entry), "環境変数だけで、自分のものと見た"
    finally:
        proc.kill()
        proc.wait(timeout=30)
    assert not marked.is_marked(proc.pid, environ=entry if marked.LINUX else None, command=needle), (
        "終わったものを、自分のものと見た")
