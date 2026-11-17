"""A0 の独立検証（verifier、5回目）: Windows の実機で出た失敗の修正（22d2fdc..b48cdf9）の確かめ。

記録は docs/verification/A0-verifier-5.md。アプリ本体は別のプロセスで、試験ごとの設定の場所で起動する。

1. 端末の Enter: ブラウザを開くコマンドが動いている間も、Enter で新しい URL が出る。
   BROWSER が「終わるまで戻らないコマンド」（Python の webbrowser は、名前を知らないコマンドの終わりを待つ）
   のとき、b48cdf9 は、ブラウザを閉じるまで Enter を読まない（22d2fdc からの後退。レビューの中1）。
   この試験は、b48cdf9 では落ちる（落ちるのが正しい。直ったら通る）。Enter を読み始める順序には頼らない
   書き方にしてある（読み方が「待たない読み方」に変わっても通る）。
2. 偽のブラウザが失敗しても、アプリ本体は動き続け、本物のブラウザを開く経路は呼ばれない（a0_fake_browser を
   通した、アプリ本体ごとの確かめ。部品だけの確かめは test_a0_fix5_watch.py）。
3. Windows の Ctrl+Break（SIGBREAK）: 止めた後は、Ctrl+C と同じに「止めました」を出して、終了コード 0 で
   終わる。
   - Windows では、本物の Ctrl+Break を送る（POのPCで流れる）。
   - Windows でない OS では、身代わりの合図（SIGUSR1）で同じ経路を流す（BREAK_ENTRY の説明）。対照（製品の
     扱いを外すと、合図の既定の動きで終わる）つき。
   - 扱いは、待ち受けの間だけ。終わったら（例外で抜けても）元に戻る。Windows でない OS、主スレッドの外、
     SIGBREAK の無い OS では、何も変えない。
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
import a0_marked as marked
import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"
WAIT = 30.0     # 起動を待つ上限（混んだ機械で落ちないように長め。合否には使わない）
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?k=([A-Za-z0-9_-]+)")
STOPPED = "circuit-sim を止めました。"
# Enter で出る行の始まり（起動のときの案内「…Enter を押すと新しい URL を出します」とは別の行）
NEW_URL = "新しい URL（1回だけ"


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


class Running:
    """アプリ本体を起動し、端末の出力を読み続ける。止め忘れても、終わりに必ず止める（stop）。"""

    def __init__(self, config: Path, command: list[str], *, stdin=subprocess.DEVNULL,
                 env: dict[str, str] | None = None, creationflags: int = 0) -> None:
        full_env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(config), "LC_ALL": "ja_JP.UTF-8",
                    "PYTHONIOENCODING": "utf-8", **(env or {})}
        self.lines: list[str] = []
        self.q: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=full_env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=stdin, creationflags=creationflags)
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
        """wanted を含む行が出るまで待つ。timeout 秒のうちに出なければ None（アプリ本体が終わっても）。"""
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
        line = self.wait_line("circuit-sim を起動しました")
        assert line is not None, "起動の URL が出ない: " + self.out
        found = URL_RE.search(line)
        assert found, line
        # 入口のページが返るまで待つ。つながるだけでは足りない（待ち受けのソケットは、アプリ本体が要求を
        # 受け始める前、合図の扱いを決める前から、つながる）
        deadline = time.monotonic() + WAIT
        while b" 200 " not in http_head(int(found.group(1)), "/").split(b"\r\n", 1)[0]:
            assert self.proc.poll() is None, "アプリ本体が起動しない: " + self.out
            assert time.monotonic() < deadline, "アプリ本体が応えない: " + self.out
            time.sleep(0.05)
        return found

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=20)


def app_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "circuit_sim.app", *args]


# ------------------------------------------------------- 1. ブラウザのコマンドが動いている間の Enter

# 終わるまで戻らないブラウザのコマンドの身代わり: 呼ばれたことを控え、止める印（browser.stop）が置かれるまで
# 動き続ける（置かれなくても、60 秒で自分で終わる）。標準入力は読まない
SLOW_BROWSER = """\
import os, sys, time

out = os.environ["A0V5_OUT"]
with open(os.path.join(out, "browser.part"), "w", encoding="utf-8") as f:
    f.write(str(os.getpid()))
os.replace(os.path.join(out, "browser.part"), os.path.join(out, "browser.pid"))
deadline = time.monotonic() + 60
while time.monotonic() < deadline and not os.path.exists(os.path.join(out, "browser.stop")):
    time.sleep(0.05)
with open(os.path.join(out, "browser.done"), "w", encoding="utf-8") as f:
    f.write("done")
"""


def test_enter_gives_a_new_url_while_the_browser_command_is_still_running(tmp_path):
    """ブラウザを開くコマンドが動いている間も、端末の Enter で新しい URL が出る。

    BROWSER に、終わるまで戻らないコマンドを置く（google-chrome-stable、brave など、Python の webbrowser が
    名前を知らないブラウザは、こう動く。webbrowser は、コマンドの終わりを待つ）。起動の文は「ここで Enter を
    押すと新しい URL を出します」と案内しているので、ブラウザを開いたまま Enter を押した人にも出す。

    10 秒は、合否の基準ではなく待つ上限（22d2fdc は、Enter の直後に出す）。"""
    script = tmp_path / "slow_browser.py"
    script.write_text(SLOW_BROWSER, encoding="utf-8")
    env = {"BROWSER": fake.command(script), "A0V5_OUT": str(tmp_path)}
    command = fake.app_argv("--port", str(free_port()))
    app = Running(tmp_path / "config", command, stdin=subprocess.PIPE, env=env)
    pid_file = tmp_path / "browser.pid"
    try:
        first = app.first_url()
        deadline = time.monotonic() + WAIT
        while not pid_file.exists():
            assert time.monotonic() < deadline, (
                "ブラウザのコマンドが呼ばれない" + fake.problem() + ": " + app.out
            )
            time.sleep(0.05)
        app.proc.stdin.write(b"\n")
        app.proc.stdin.flush()
        line = app.wait_line(NEW_URL, timeout=10)
        still_running = not (tmp_path / "browser.done").exists()
        assert line is not None and still_running, (
            "ブラウザのコマンドが動いている間は、Enter を押しても新しい URL が出ない: " + app.out
        )
        assert URL_RE.search(line).group(2) != first.group(2), "新しい URL の番号が、最初の番号と同じ"
        assert fake.read_notes() == [], "偽のブラウザが失敗した、または本物の経路が呼ばれた"
    finally:
        (tmp_path / "browser.stop").write_text("stop", encoding="utf-8")
        app.stop()
        if pid_file.exists():       # 身代わりのブラウザを残さない（印を見て自分で終わるが、念のため止める）
            # 番号は、身代わりが終わって受け取られた後なら使い回されうるので、印（Linux は環境変数、どの OS
            # でも引数の場所）で確かめてから止める（D-078。8回目の検証で直した）
            with contextlib.suppress(OSError, ValueError):
                marked.kill_if_marked(int(pid_file.read_text(encoding="utf-8")),
                                      environ=f"A0V5_OUT={tmp_path}" if marked.LINUX else None,
                                      command=f"{tmp_path.name}/{script.name}")


# ------------------------------------------------------------------ 2. 偽のブラウザが失敗したとき


def test_app_keeps_running_and_the_real_browser_is_not_tried_when_the_fake_browser_fails(tmp_path):
    """偽のブラウザが 0 でない終了コードで終わっても、本物のブラウザを開く経路は呼ばれない。

    アプリ本体は動き続け、起動の番号も使えるまま（転送用の HTML も残っている）。"""
    script, called = tmp_path / "failing_browser.py", tmp_path / "called.txt"
    script.write_text(
        "import sys\n"
        f"with open({str(called)!r}, 'w', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]))\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    config = tmp_path / "config"
    app = Running(config, fake.app_argv("--port", str(free_port())), env={"BROWSER": fake.command(script)})
    try:
        found = app.first_url()
        deadline = time.monotonic() + WAIT
        while not fake.noted("fake-failed"):
            assert time.monotonic() < deadline, "偽のブラウザが失敗した記録が残らない: " + app.out
            time.sleep(0.05)
        target = called.read_text(encoding="utf-8")
        assert target.startswith("file:") and found.group(2) not in target, target
        assert fake.read_notes() == [("fake-failed", target)], "本物のブラウザを開く経路が呼ばれた"
        assert app.proc.poll() is None, "アプリ本体が止まった: " + app.out
        assert len(list(config.glob("open-*.html"))) == 1, "転送用の HTML が残っていない"
        head = http_head(int(found.group(1)), f"/?k={found.group(2)}")
        assert re.search(rb"(?im)^set-cookie:\s*circuit_sim_key=", head), head[:300]
        fake.clear_notes()      # わざと失敗させた記録（fake-failed）は、確かめたので消す
    finally:
        app.stop()


# ------------------------------------------------------------------ 3. Windows の Ctrl+Break

# Windows の Ctrl+Break の経路を、Windows でない OS で流すための入口。製品のコードは変えず、起動の前に
# 差し替える:
# - signal.SIGBREAK（Windows にしか無い名前）に、身代わりの合図 SIGUSR1 を当てる
# - uvicorn が受け持つ合図に、身代わりを足す（uvicorn は、win32 でだけ SIGBREAK を受け持つ。
#   uvicorn/server.py の HANDLED_SIGNALS）
# - cli.WINDOWS を真にする（製品の Windows の分かれ目）
# 同じ経路と言える理由: uvicorn は、止め終えた後に、受けた合図の扱いを元に戻してから、同じ合図を出し直す
# （Server.capture_signals の signal.raise_signal）。Python の raise_signal は、C の raise を呼んだ後、
# Python の扱いをその場で実行する（Modules/signalmodule.c の signal_raise_signal_impl）。元の扱いが製品の
# 決めたもの（KeyboardInterrupt を出す）なら、main の終わりまで進む。ここまでは OS によらない。違うのは、
# 元の扱いが「既定」のままのときの終わり方だけ: Windows は終了コード 3（PO の PC で確かめた）、POSIX は
# 合図で終わる。
# 最初の引数で、流し方を選ぶ:
#   with-the-fix                そのまま
#   without-the-fix             対照。製品の扱い（cli.break_as_interrupt）を外す
#   break-before-the-server     待ち受けを始める直前（製品の扱いの中、uvicorn が合図を受け持つ前）に合図を出す
BREAK_ENTRY = """\
import contextlib, signal, sys

import uvicorn.server

from circuit_sim.app import cli

STAND_IN = signal.SIGUSR1
signal.SIGBREAK = STAND_IN
uvicorn.server.HANDLED_SIGNALS += (STAND_IN,)
cli.WINDOWS = True
mode = sys.argv.pop(1)
if mode == "without-the-fix":
    cli.break_as_interrupt = lambda windows: contextlib.nullcontext()
elif mode == "break-before-the-server":
    run = cli.GuardedServer.run

    def run_after_a_break(self, sockets=None):
        signal.raise_signal(STAND_IN)
        return run(self, sockets=sockets)

    cli.GuardedServer.run = run_after_a_break
raise SystemExit(cli.main(sys.argv[1:]))
"""

only_with_a_stand_in = pytest.mark.skipif(
    WINDOWS or not hasattr(signal, "SIGUSR1"),
    reason="身代わりの合図（SIGUSR1）は POSIX だけ。Windows では、本物の Ctrl+Break の試験が流れる",
)


def start_with_stand_in(tmp_path: Path, mode: str) -> Running:
    command = [sys.executable, "-c", BREAK_ENTRY, mode, "--no-browser", "--port", str(free_port())]
    return Running(tmp_path / "config", command)


def end_of(app: Running, timeout: float = 20.0) -> tuple[int, str]:
    """アプリ本体が終わるのを待ち、（終了コード、端末の出力の全部）を返す。"""
    code = app.proc.wait(timeout=timeout)
    deadline = time.monotonic() + 10
    while app.q.get(timeout=max(0.01, deadline - time.monotonic())) is not None:
        pass
    return code, app.out


@only_with_a_stand_in
@pytest.mark.parametrize("signals", [
    ["break"], ["break", "break"], ["int", "break"], ["break", "int"], ["break", "break", "break"],
], ids=["break", "break-twice", "ctrl-c-then-break", "break-then-ctrl-c", "break-three-times"])
def test_break_stops_like_ctrl_c_with_exit_code_0(tmp_path, signals):
    """Ctrl+Break（の身代わり）で止めると、「止めました」を1回出して、終了コード 0 で終わる。

    続けて押しても、Ctrl+C と組み合わせても同じ（続けて送る。止めている最中に届く）。"""
    app = start_with_stand_in(tmp_path, "with-the-fix")
    try:
        app.first_url()
        for name in signals:
            app.proc.send_signal(signal.SIGUSR1 if name == "break" else signal.SIGINT)
        code, out = end_of(app)
    finally:
        app.stop()
    assert code == 0 and out.count(STOPPED) == 1 and "Traceback" not in out, (code, out)


@only_with_a_stand_in
def test_break_right_before_the_server_listens_still_ends_with_exit_code_0(tmp_path):
    """待ち受けを始める直前（uvicorn が合図を受け持つ前）に Ctrl+Break（の身代わり）が届いても、
    後始末をして「止めました」を出し、終了コード 0 で終わる（起動の途中での合図）。"""
    app = start_with_stand_in(tmp_path, "break-before-the-server")
    try:
        code, out = end_of(app)
    finally:
        app.stop()
    assert code == 0 and out.count(STOPPED) == 1 and "Traceback" not in out, (code, out)
    assert "circuit-sim を起動しました" in out


@only_with_a_stand_in
def test_without_the_handling_the_break_ends_by_the_default_action(tmp_path):
    """対照: 製品の扱い（cli.break_as_interrupt）が無いと、止め終えた後に、合図の既定の動きで終わる。

    「止めました」は出ず、終了コードは 0 にならない（POSIX は合図 SIGUSR1 で終わる。Windows の Ctrl+Break は
    終了コード 3。PO の PC で確かめた）。上の試験が、扱いの有無を見分けられることの確かめ。"""
    app = start_with_stand_in(tmp_path, "without-the-fix")
    try:
        app.first_url()
        app.proc.send_signal(signal.SIGUSR1)
        code, out = end_of(app)
    finally:
        app.stop()
    assert code == -signal.SIGUSR1 and STOPPED not in out, (code, out)


def test_break_handling_is_only_for_the_wait_and_only_on_windows(monkeypatch):
    """break_as_interrupt: Windows の主スレッドでだけ、中にいる間だけ、SIGBREAK が KeyboardInterrupt になる。

    終わったら（例外で抜けても）、入る前の扱いに戻る（入る前が、既定でも、自分で決めた扱いでも）。
    Windows でない OS、主スレッドの外、SIGBREAK の無い OS では、何も変えない。"""
    from circuit_sim.app import cli  # noqa: PLC0415  製品の部品を、そのまま確かめる

    real = getattr(signal, "SIGBREAK", None)
    stand_in = real or getattr(signal, "SIGUSR2", None)
    if stand_in is None:
        pytest.skip("身代わりにできる合図が無い")
    assert threading.current_thread() is threading.main_thread()
    before = signal.getsignal(stand_in)

    def handler_inside(windows: bool) -> object:
        with cli.break_as_interrupt(windows):
            return signal.getsignal(stand_in)

    def mine(signum, frame):  # 入る前に、自分で決めてあった扱い
        raise AssertionError("呼ばれない")

    try:
        if real is None:
            # SIGBREAK の無い OS: Windows の分かれ目に入っても、何も変えず、落ちない
            assert handler_inside(True) is before
            monkeypatch.setattr(signal, "SIGBREAK", stand_in, raising=False)
        assert handler_inside(False) is before, "Windows でない OS で、合図の扱いを変えた"
        seen: list[object] = []
        thread = threading.Thread(target=lambda: seen.append(handler_inside(True)))
        thread.start()
        thread.join(timeout=20)
        assert seen == [before], "主スレッドの外から、合図の扱いを変えた（または落ちた）"
        for previous in (signal.SIG_DFL, mine):
            signal.signal(stand_in, previous)
            assert handler_inside(True) is signal.default_int_handler
            assert signal.getsignal(stand_in) is previous, "出た後に、入る前の扱いに戻っていない"
            with pytest.raises(KeyboardInterrupt):
                with cli.break_as_interrupt(True):
                    # 扱いが決まっていることを確かめてから出す（既定のまま出すと、試験のプロセスが終わる）
                    assert signal.getsignal(stand_in) is signal.default_int_handler
                    signal.raise_signal(stand_in)   # 出し直された合図は、KeyboardInterrupt になる
                    time.sleep(5)
            assert signal.getsignal(stand_in) is previous, "例外で抜けた後に、入る前の扱いに戻っていない"
    finally:
        signal.signal(stand_in, before)


@pytest.mark.skipif(not WINDOWS, reason="Windows の本物の Ctrl+Break（POのPCで流れる）")
@pytest.mark.parametrize("times", [1, 2], ids=["break", "break-twice"])
def test_ctrl_break_on_windows_stops_with_exit_code_0(tmp_path, times):
    """Windows: Ctrl+Break で止めると、「止めました」を出して、終了コード 0 で終わる（直す前は 3）。"""
    app = Running(tmp_path / "config", app_command("--no-browser", "--port", str(free_port())),
                  creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        app.first_url()
        try:
            for _ in range(times):
                app.proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")
        code, out = end_of(app)
    finally:
        app.stop()
    assert code == 0 and out.count(STOPPED) == 1 and "Traceback" not in out, (code, out)
