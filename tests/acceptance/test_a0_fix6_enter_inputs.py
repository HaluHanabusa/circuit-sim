"""A0 の独立検証（verifier、6回目）その1: 端末の Enter を、アプリ本体ごと確かめる（80da2ea..9f594c5）。

記録は docs/verification/A0-verifier-6.md。アプリ本体は別のプロセスで、試験ごとの設定の場所で起動する。
どの試験も、Windows でも流れる（POSIX だけの分は、そう書いてある）。標準入力は、ことわりが無ければパイプ
（ほかのプログラムから起動された形。Windows では、製品の「待たない読み方」を通る）。

確かめること（5回目の記録の「次の修正で確かめ直す項目」の 1〜3）:
1. ブラウザがどうなっても、Enter で新しい URL が出る: ブラウザのコマンドが終わるまで戻らない、すぐ戻る
   （終わりに & を付けた指定）、失敗する、開く呼び出しが「開けなかった」と答える・例外を出す、
   転送用の HTML を書けない、ブラウザを開かない（--no-browser）。出た番号は使える。
2. 起動の文が出る前に押した Enter を捨てない。
3. Enter 1回につき、新しい URL は1つ（文字を打ってからの Enter、CR LF でも1つ）。
4. Enter を読めない入力（NUL、ファイル、閉じた入力）と、入力の終わり（書く側が閉じたパイプ）でも、
   アプリ本体は動き続ける。押していない Enter を作らない。ファイルの中の改行を Enter として読まない。
   入力の終わりの後に、CPU を使い続けない。

本物のブラウザは開かない: ブラウザのコマンドを使う起動は a0_fake_browser を通す。そのほかは、起動の前に
webbrowser.open を差し替える（MODE_ENTRY。本物を開く経路そのものが無い）。製品のコードは変えない。

待つ時間（WAIT、10 秒など）は、合否の基準ではなく上限。合否は、端末の文、番号、プロセスが動いているか。
"""
from __future__ import annotations

import contextlib
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import a0_fake_browser as fake
import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
WAIT = 30.0
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?k=([A-Za-z0-9_-]+)")
STARTED = "circuit-sim を起動しました"
# Enter で出る行の始まり（起動のときの案内「…Enter を押すと新しい URL を出します」とは別の行）
NEW_URL = "新しい URL（1回だけ"
ENTER_HINT = "ここで Enter を押すと新しい URL を出します"
RESTART_HINT = "circuit-sim を起動し直してください"
COULD_NOT_OPEN = "ブラウザを開けませんでした"
STOPPED = "circuit-sim を止めました。"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_head(port: int, path: str) -> bytes:
    """GET の応答の見出し（つながらない、途中で切れたときは空）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            s.sendall(request.encode("ascii"))
            data = b""
            while chunk := s.recv(65536):
                data += chunk
    except OSError:
        return b""
    return data.split(b"\r\n\r\n", 1)[0]


def answers(port: int) -> bool:
    return b" 200 " in http_head(port, "/").split(b"\r\n", 1)[0]


class Running:
    """アプリ本体を起動し、端末の出力を読み続ける（待つときは、上限つき）。"""

    def __init__(self, config: Path, command: list[str], *, stdin=subprocess.DEVNULL,
                 env: dict[str, str] | None = None) -> None:
        full_env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(config), "LC_ALL": "ja_JP.UTF-8",
                    "PYTHONIOENCODING": "utf-8", **(env or {})}
        self.lines: list[str] = []
        self.q: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=full_env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=stdin)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace")
            self.lines.append(line)
            self.q.put(line)
        self.q.put(None)

    @property
    def out(self) -> str:
        return "".join(self.lines)

    def wait_line(self, wanted: str, timeout: float = WAIT) -> str | None:
        """wanted を含む、まだ受け取っていない行が出るまで待つ。timeout 秒のうちに出なければ None。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.q.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if line is None:
                self.q.put(None)
                return None
            if wanted in line:
                return line

    def first_url(self) -> re.Match[str]:
        """起動の URL。入口のページが返るまで待つ（つながるだけでは足りない）。"""
        deadline = time.monotonic() + WAIT
        while not any(STARTED in line for line in self.lines):
            assert self.proc.poll() is None, "アプリ本体が終わった: " + self.out
            assert time.monotonic() < deadline, "起動の URL が出ない: " + self.out
            time.sleep(0.05)
        found = URL_RE.search(next(line for line in self.lines if STARTED in line))
        assert found, self.out
        while not answers(int(found.group(1))):
            assert self.proc.poll() is None, "アプリ本体が起動しない: " + self.out
            assert time.monotonic() < deadline, "アプリ本体が応えない: " + self.out
            time.sleep(0.05)
        return found

    def has_line(self, text: str, timeout: float = 10.0) -> bool:
        """text を含む行が出たか（出るまで、timeout 秒まで待つ。wait_line の受け取りの位置は動かさない）。"""
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.lines):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    def press(self, data: bytes = b"\n") -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=20)
        if self.proc.stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                self.proc.stdin.close()


# 終わるまで戻らないブラウザのコマンドの身代わり: 呼ばれたことを控え、止める印（browser.stop）が置かれるまで
# 動き続ける（置かれなくても、60 秒で自分で終わる）。標準入力は読まない
SLOW_BROWSER = """\
import os, sys, time

out = os.environ["A0V6_OUT"]
with open(os.path.join(out, "browser.part"), "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))
os.replace(os.path.join(out, "browser.part"), os.path.join(out, "browser.pid"))
deadline = time.monotonic() + 60
while time.monotonic() < deadline and not os.path.exists(os.path.join(out, "browser.stop")):
    time.sleep(0.05)
with open(os.path.join(out, "browser.done"), "w", encoding="utf-8") as f:
    f.write("done")
"""

FAILING_BROWSER = """\
import os, sys

with open(os.path.join(os.environ["A0V6_OUT"], "browser.called"), "w", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]))
sys.exit(7)
"""

# アプリ本体（python -m circuit_sim.app と同じ）を、本物のブラウザを開く経路を無くしてから始める入口。
# 製品のコードは変えない。起動の前に、A0V6_MODE で決めた所だけを差し替える:
#   open-says-no       webbrowser.open が「開けなかった」と答える（画面の無い機械と同じ）
#   open-raises        webbrowser.open が例外を出す
#   page-unwritable    転送用の HTML を書けない（LaunchPages.write が、空きが無いときと同じ OSError）。
#                      webbrowser.open は呼ばれないはず（呼ばれたら控える。本物は開かない）
# どの場合も、webbrowser.open が呼ばれたら、渡された場所を open.called に控える
MODE_ENTRY = r'''
import errno, os, runpy, webbrowser

mode = os.environ["A0V6_MODE"]
out = os.environ["A0V6_OUT"]


def fake_open(target, *args, **kwargs):
    with open(os.path.join(out, "open.part"), "w", encoding="utf-8") as f:
        f.write(str(target))
    os.replace(os.path.join(out, "open.part"), os.path.join(out, "open.called"))
    if mode == "open-raises":
        raise RuntimeError("no browser here")
    return mode == "page-unwritable"


webbrowser.open = fake_open
if mode == "page-unwritable":
    from circuit_sim.app import cli

    def cannot_write(self, number, url, theme=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    cli.LaunchPages.write = cannot_write
elif mode not in ("open-says-no", "open-raises"):
    raise SystemExit("unknown mode: " + mode)
runpy.run_module("circuit_sim.app", run_name="__main__", alter_sys=True)
'''

# 標準入力の fd を閉じてから、アプリ本体に入れ替わる入口（POSIX だけ。入力そのものが無い起動）
CLOSED_FD_ENTRY = """\
import os, sys

os.close(0)
os.execv(sys.executable, [sys.executable, "-m", "circuit_sim.app", *sys.argv[1:]])
"""

BROWSER_KINDS = ["command-waits", "command-returns-at-once", "command-fails", "open-says-no", "open-raises",
                 "page-unwritable", "no-browser"]


def start(tmp_path: Path, kind: str, *, stdin=subprocess.PIPE) -> Running:
    """kind の形で、アプリ本体を起動する（本物のブラウザは、どの形でも開かない）。"""
    port = ["--port", str(free_port())]
    env = {"A0V6_OUT": str(tmp_path)}
    config = tmp_path / "config"
    if kind == "no-browser":
        return Running(config, [sys.executable, "-m", "circuit_sim.app", "--no-browser", *port],
                       stdin=stdin, env=env)
    if kind.startswith("command-"):
        script = tmp_path / "browser.py"
        script.write_text(FAILING_BROWSER if kind == "command-fails" else SLOW_BROWSER, encoding="utf-8")
        # 終わりに & を付けた指定は、webbrowser が起動してすぐ戻る（Lib/webbrowser.py の BackgroundBrowser）
        env["BROWSER"] = fake.command(script) + (" &" if kind == "command-returns-at-once" else "")
        return Running(config, fake.app_argv(*port), stdin=stdin, env=env)
    env["A0V6_MODE"] = kind
    return Running(config, [sys.executable, "-c", MODE_ENTRY, *port], stdin=stdin, env=env)


def stop_all(app: Running, tmp_path: Path) -> None:
    """アプリ本体と、身代わりのブラウザを止める（印を見て自分で終わるが、念のため番号でも止める）。"""
    (tmp_path / "browser.stop").write_text("stop", encoding="utf-8")
    app.stop()
    pid_file = tmp_path / "browser.pid"
    if pid_file.exists() and not (tmp_path / "browser.done").exists():
        deadline = time.monotonic() + 5
        while not (tmp_path / "browser.done").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not (tmp_path / "browser.done").exists():
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)


def wait_for(path: Path, app: Running, what: str) -> None:
    deadline = time.monotonic() + WAIT
    while not path.exists():
        assert app.proc.poll() is None, f"アプリ本体が終わった（{what}）: " + app.out
        assert time.monotonic() < deadline, f"{what}{fake.problem()}: " + app.out
        time.sleep(0.05)


# ------------------------------------------------------------ 1. ブラウザがどうなっても、Enter が効く


@pytest.mark.parametrize("kind", BROWSER_KINDS)
def test_enter_gives_a_usable_new_url_whatever_happens_to_the_browser(tmp_path, kind):
    """ブラウザがどうなっても、端末の Enter で新しい URL が出る。2回押せば2つ出て、出た番号は使える。

    起動の文は、どの場合も「ここで Enter を押すと新しい URL を出します」と案内する。
    - command-waits: BROWSER のコマンドが終わるまで戻らない。コマンドが動いている間に出る（5回目の中1）
    - command-returns-at-once: 終わりに & を付けた指定。コマンドは、アプリ本体が Enter を待っている最中に
      起動される（Windows では、パイプを読んで待っていると、ここでコマンドが始まれない）
    - command-fails、open-says-no、open-raises、page-unwritable: 開けなかったとき
    - no-browser: 開かないとき"""
    app = start(tmp_path, kind)
    try:
        first = app.first_url()
        port = int(first.group(1))
        assert app.has_line(ENTER_HINT), "Enter の案内が出ない: " + app.out
        if kind in ("command-waits", "command-returns-at-once"):
            wait_for(tmp_path / "browser.pid", app, "ブラウザのコマンドが始まらない")
        elif kind == "command-fails":
            wait_for(tmp_path / "browser.called", app, "ブラウザのコマンドが呼ばれない")
        elif kind in ("open-says-no", "open-raises", "page-unwritable"):
            line = app.wait_line(COULD_NOT_OPEN)
            assert line is not None and first.group(0) in line, (
                "開けなかった案内に、起動の URL が無い: " + app.out
            )
            if kind == "page-unwritable":
                assert not (tmp_path / "open.called").exists(), "開く場所が無いのに、ブラウザを開こうとした"
            else:
                target = (tmp_path / "open.called").read_text(encoding="utf-8")
                assert target.startswith("file:") and first.group(2) not in target, target
        numbers = [first.group(2)]
        for nth in (1, 2):
            app.press()
            line = app.wait_line(NEW_URL, timeout=10)
            assert line is not None, f"{nth} 回目の Enter で、新しい URL が出ない: " + app.out
            found = URL_RE.search(line)
            assert found and int(found.group(1)) == port, line
            assert found.group(2) not in numbers, "新しい URL の番号が、前と同じ: " + app.out
            numbers.append(found.group(2))
        if kind == "command-waits":
            assert not (tmp_path / "browser.done").exists(), (
                "ブラウザのコマンドが終わってから、新しい URL が出た"
            )
        head = http_head(port, f"/?k={numbers[-1]}")
        assert re.search(rb"(?im)^set-cookie:\s*circuit_sim_key=", head), ("新しい番号が使えない", head[:300])
        assert app.proc.poll() is None, "アプリ本体が止まった: " + app.out
        assert "Traceback" not in app.out, app.out
        if kind == "command-fails":
            # 失敗の記録は、偽のブラウザが終わった後に残る（Enter の往復より遅いことがある）
            deadline = time.monotonic() + WAIT
            while not fake.noted("fake-failed"):
                assert time.monotonic() < deadline, "偽のブラウザが失敗した記録が残らない: " + app.out
                time.sleep(0.05)
            target = (tmp_path / "browser.called").read_text(encoding="utf-8")
            assert fake.read_notes() == [("fake-failed", target)], "本物のブラウザを開く経路が呼ばれた"
            fake.clear_notes()      # わざと失敗させた記録は、確かめたので消す
        else:
            assert fake.read_notes() == [], "偽のブラウザが失敗した、または本物の経路が呼ばれた"
    finally:
        stop_all(app, tmp_path)


# ------------------------------------------------------------ 2. 先に押した Enter


@pytest.mark.parametrize("kind", ["no-browser", "command-waits"])
def test_enter_pressed_before_the_app_is_ready_is_not_lost(tmp_path, kind):
    """起動の文が出る前に押した Enter（先に送ってあった改行）も、捨てずに、新しい URL を出す。

    ほかのプログラムが、起動と同時に改行を送る形。読み始める時期を変える直し方で、失われやすい所。"""
    app = start(tmp_path, kind)
    try:
        app.press()     # 起動の直後。アプリ本体は、まだ何も出していない
        first = app.first_url()
        line = app.wait_line(NEW_URL, timeout=10)
        assert line is not None, "先に押した Enter で、新しい URL が出ない: " + app.out
        assert URL_RE.search(line).group(2) != first.group(2)
        if kind == "command-waits":
            wait_for(tmp_path / "browser.pid", app, "ブラウザのコマンドが始まらない")
        assert app.proc.poll() is None, "アプリ本体が止まった: " + app.out
    finally:
        stop_all(app, tmp_path)


# ------------------------------------------------------------ 3. Enter 1回につき、1つ


def test_each_enter_gives_exactly_one_new_url(tmp_path):
    """Enter 1回につき、新しい URL は1つ。文字を打ってからの Enter でも、CR LF（Windows の改行）でも1つ。

    1回の Enter で2つ出ても、どの番号も使える（使うか、期限が切れるまで有効）が、端末の案内が読みにくく
    なる。22d2fdc も、1回につき1つ。1 秒は、余分な行が出ないことを見るための待ち（合否は、行の数）。"""
    app = start(tmp_path, "no-browser")
    try:
        first = app.first_url()
        numbers = [first.group(2)]
        for data in (b"\n", b"\n", b"abc\n", b"\r\n"):
            app.press(data)
            line = app.wait_line(NEW_URL, timeout=10)
            assert line is not None, f"{data!r} を送っても、新しい URL が出ない: " + app.out
            numbers.append(URL_RE.search(line).group(2))
            extra = app.wait_line(NEW_URL, timeout=1.0)
            assert extra is None, f"{data!r} の1回で、新しい URL が2つ出た: " + app.out
        assert len(set(numbers)) == 5, numbers
    finally:
        stop_all(app, tmp_path)


# ------------------------------------------------------------ 4. 読めない入力と、入力の終わり


def cpu_seconds(pid: int) -> float:
    """Linux: プロセスが使った CPU の時間（秒）。/proc/<pid>/stat の utime と stime。"""
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


UNREADABLE = ["null", "file", "closed-pipe"] + ([] if WINDOWS else ["closed-fd"])


@pytest.mark.parametrize("kind", UNREADABLE)
def test_app_keeps_running_and_invents_no_enter_when_the_input_gives_none(tmp_path, kind):
    """Enter を読めない入力でも、入力が終わっても、アプリ本体は動き続け、押していない Enter を作らない。

    - null: NUL（/dev/null）。Enter は読めないので、起動し直す方法を案内する
    - file: 改行だけが入ったファイル。ファイルの中の改行を、Enter として読まない（案内は null と同じ）
    - closed-pipe: 書く側がすぐ閉じたパイプ（入力の終わり）。パイプなので案内は Enter のまま。読むのをやめる
    - closed-fd: 標準入力そのものが無い（POSIX だけ）
    入力の終わりの後に、読み続けて CPU を使い切らない（Linux で、3 秒の間の CPU の時間を見る。上限 0.5 秒は、
    回り続けたとき（1 つの CPU を使い切り、3 秒近くになる）と見分けるための値。何もしていないアプリ本体は
    0.05 秒ほど）。POSIX では、その後も Ctrl+C（SIGINT）で、終了コード 0 で止まる。"""
    port = ["--no-browser", "--port", str(free_port())]
    command = [sys.executable, "-m", "circuit_sim.app", *port]
    config = tmp_path / "config"
    held = None
    if kind == "null":
        app = Running(config, command, stdin=subprocess.DEVNULL)
    elif kind == "file":
        source = tmp_path / "input.txt"
        source.write_bytes(b"\n\n\n")
        held = open(source, "rb")  # noqa: SIM115  起動した後に閉じる
        app = Running(config, command, stdin=held)
    elif kind == "closed-pipe":
        app = Running(config, command, stdin=subprocess.PIPE)
        app.proc.stdin.close()
    else:
        app = Running(config, [sys.executable, "-c", CLOSED_FD_ENTRY, *port], stdin=subprocess.DEVNULL)
    try:
        first = app.first_url()
        wanted, other = (ENTER_HINT, RESTART_HINT) if kind == "closed-pipe" else (RESTART_HINT, ENTER_HINT)
        assert app.has_line(wanted), "新しい URL の出し方の案内が違う: " + app.out
        assert not any(other in line for line in app.lines), app.out
        before = cpu_seconds(app.proc.pid) if LINUX else 0.0
        time.sleep(3.0)
        used = cpu_seconds(app.proc.pid) - before if LINUX else 0.0
        assert app.proc.poll() is None, "アプリ本体が止まった: " + app.out
        assert NEW_URL not in app.out, "押していない Enter で、新しい URL が出た: " + app.out
        assert "Traceback" not in app.out, app.out
        assert answers(int(first.group(1))), "アプリ本体が応えなくなった"
        assert used < 0.5, f"入力を読めない 3 秒の間に、CPU を {used:.2f} 秒使った（読み続けている）"
        if not WINDOWS:
            app.proc.send_signal(signal.SIGINT)
            assert app.proc.wait(timeout=20) == 0, app.out
            deadline = time.monotonic() + 10
            while STOPPED not in app.out:
                assert time.monotonic() < deadline, "「止めました」が出ない: " + app.out
                time.sleep(0.05)
    finally:
        app.stop()
        if held is not None:
            held.close()


# ------------------------------------------------------------ 5. 端末の文は、行ごとに混ざらない（軽微）


class SlowTerminal:
    """端末の身代わり: 書かれたものを順に控える。1回書くたびに少し待つ（その間に、ほかのスレッドが書ける）。

    本物でも、書くたびに OS へ渡す出力（端末、PYTHONUNBUFFERED=1 や -u のパイプ）では、書いている間に
    ほかのスレッドが動く。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        time.sleep(0.05)
        return len(text)

    def flush(self) -> None:
        pass


@pytest.mark.xfail(strict=True, reason=(
    "軽微（A0 の検証 6回目。22d2fdc からある）: 端末の文を出す say が、文と改行を別々に書くので、2つの"
    "スレッド（止める知らせと、Enter の新しい URL など）の文が1行に混ざることがある"
))
def test_messages_from_two_threads_do_not_run_into_each_other(monkeypatch):
    """端末の文は、2つのスレッドから同時に出ても、文ごとにまとまって出る（1行に混ざらない）。

    アプリ本体は、主スレッド（止めるときの知らせ）、Enter を読むスレッド（新しい URL）、要求を受ける
    スレッド（使い終わった URL の案内）、ブラウザを開くスレッド（開けなかった案内）から、端末に文を出す。
    混ざると、たとえば「…もう一度 Ctrl+C を押してください。新しい URL（…）: http://…」と1行になり、
    次の行が空になる（この環境の試験で、実際に出た。PYTHONUNBUFFERED=1 のとき）。
    身代わりの端末で、片方が書いている最中に、もう片方が書く形を作る（待ちの長さは、順序を決めるため）。"""
    from circuit_sim.app import cli  # noqa: PLC0415  製品の部品を、そのまま確かめる

    terminal = SlowTerminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    first = threading.Thread(target=cli.say, args=("warning line 1\nwarning line 2",))
    second = threading.Thread(target=cli.say, args=("new url line",))
    first.start()
    time.sleep(0.02)    # first が書いている最中に
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)
    monkeypatch.undo()
    written = "".join(terminal.chunks)
    assert sorted(written.splitlines()) == ["new url line", "warning line 1", "warning line 2"], (
        f"2つのスレッドの文が混ざった: {written!r}"
    )
