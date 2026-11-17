"""起動コマンド circuit-sim（circuit_sim/app/cli.py）の試験。別のプロセスで起動して確かめる。

設定の場所は、試験ごとの一時フォルダ（CIRCUIT_SIM_CONFIG_DIR）。端末の文言の言語は LC_ALL で決める。
起動したときの URL（/?k=…）に載るのは、一度だけ使える起動の番号（合言葉ではない）。試験も画面と同じく、
その URL を1回開いてクッキーを受け取る。2つ目の URL が要るときは、端末に Enter を送って出させる。

起動の決まり（tests/app_procs.py）:
- アプリ本体は launch（--no-browser つき）か launch_with_fake_browser で起動する。どちらも、起動したものを
  控えに入れる。試験の終わりに動いていたら、見張り（no_process_left_behind）が止めて、その試験を失敗にする。
  「起動に失敗して終わるはず」の起動は run_to_end で流す（終わらなければ、止めてから落とす）。
- ブラウザを開く起動（--no-browser を付けない起動）は、必ず launch_with_fake_browser。偽のブラウザが
  失敗しても、OS の既定のブラウザ（本物）へ進ませない見張りを付けて起動する（BROWSER_GUARD の説明）。
  launch は、--no-browser の無い起動を断る。
- 端末の出力は、待つ上限つきで読む（proc.next_line、proc.wait_line）。製品が何も出さなくなる後退が入っても、
  試験は止まらずに落ちる。

Windows だけの分岐を、OS の呼び出しを差し替えて Linux でも流す試験は、差し替える値を実機の値か、
Microsoft の文書・CPython のソースの値に合わせる。出どころは、それぞれの試験に書く。
"""
from __future__ import annotations

import errno
import http.cookiejar
import json
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import app_procs as procs
import pytest

from circuit_sim.app import cli
from circuit_sim.app.cli import (
    DEFAULT_PORT,
    FALLBACK_PORTS,
    MESSAGES,
    RESTART_WAIT_SECONDS,
    Texts,
    bind_any,
    bind_failure_text,
    port_problem,
    terminal_lang,
)
from circuit_sim.app.config import Settings
from circuit_sim.app.server import Launches, create_app

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
START_LIMIT_S = 5.0
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/(\?k=[A-Za-z0-9_-]+)?")


@pytest.fixture(autouse=True)
def no_process_left_behind():
    """その試験が起動したプロセスが、試験の終わりに動いていたら、止めて（子も）、その試験を失敗にする。"""
    start = procs.mark()
    yield
    left = procs.reap(start)
    if left:
        pytest.fail("この試験が起動したプロセスが、試験の終わりにも動いていた（止めた）。試験の中で必ず止める:\n"
                    + "".join(f"  {line}\n" for line in left), pytrace=False)


def app_env(config: Path, lang: str = "ja_JP.UTF-8", extra: dict | None = None) -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(config), "LC_ALL": lang,
            **(extra or {})}


def launch(*args: str, config: Path, lang: str = "ja_JP.UTF-8", env: dict | None = None,
           stdin=subprocess.DEVNULL, creationflags: int = 0) -> procs.Launched:
    """アプリ本体を起動する（python -m circuit_sim.app）。ブラウザを開かない起動だけ。

    引数の誤りや --help で終わるはずの起動にも --no-browser を付ける（製品が起動してしまっても、本物の
    ブラウザが、生きている起動の番号を持つ HTML を開かないように）。"""
    assert "--no-browser" in args, "ブラウザを開く起動は、launch_with_fake_browser で起動する"
    return procs.Launched([sys.executable, "-m", "circuit_sim.app", *args], cwd=ROOT,
                          env=app_env(config, lang, env), stdin=stdin, creationflags=creationflags)


def run_to_end(*args: str, config: Path, lang: str = "ja_JP.UTF-8", env: dict | None = None,
               timeout: float = 30.0) -> tuple[int, str]:
    """終わるはずの起動（--help、引数の誤り、起動に失敗する起動）を流し、（終了コード、出力）を返す。

    timeout 秒たっても終わらなければ（期待と違って、起動に成功して待ち受けている）、止めてから落とす。"""
    proc = launch(*args, config=config, lang=lang, env=env)
    try:
        return proc.end(timeout)
    finally:
        proc.stop()


# ブラウザを開く起動のときの見張り（アプリ本体を始める前に流す。製品のコードは変えない）。
#
# webbrowser は、環境変数 BROWSER の候補を、OS の既定の候補の「前」に足すだけで、既定の候補は残す
# （CPython 3.13 の Lib/webbrowser.py の register_standard_browsers）。open は、候補を前から順に試し、
# 偽を返した候補（起動できない、0 でない終了コード）の次へ進む（同じファイルの open と GenericBrowser.open）。
# だから、BROWSER に偽のブラウザを置くだけでは、偽が失敗したときに、OS の既定のブラウザ（Windows は
# os.startfile、macOS は osascript、Linux は xdg-open など）が、生きている起動の番号を持つ HTML を開く。
#
# ここでは、「BROWSER の候補だけを試し、結果がどうでも、そこで終わりにする」候補を先頭に登録する
# （webbrowser.register の preferred と webbrowser.get。どちらも文書にある使い方）。偽が失敗したら、
# CIRCUIT_SIM_TEST_BROWSER_FAILED のファイルに控える（試験が、失敗を理由つきで知らせる）。
BROWSER_GUARD = """\
import os, runpy, webbrowser


class OnlyTheFake:
    def open(self, url, new=0, autoraise=True):
        ok = False
        try:
            ok = bool(webbrowser.get(os.environ["BROWSER"]).open(url, new, autoraise))
        finally:
            if not ok:
                with open(os.environ["CIRCUIT_SIM_TEST_BROWSER_FAILED"], "a", encoding="utf-8") as f:
                    f.write(url + "\\n")
        return True


webbrowser.register("circuit-sim-test-guard", None, OnlyTheFake(), preferred=True)
if os.environ.get("CIRCUIT_SIM_TEST_BROWSER_OPEN"):
    print(webbrowser.open(os.environ["CIRCUIT_SIM_TEST_BROWSER_OPEN"]))     # 見張りそのものの試験
else:
    runpy.run_module("circuit_sim.app", run_name="__main__")                # python -m circuit_sim.app と同じ
"""


def browser_command(script: Path, background: bool = False) -> str:
    """BROWSER に渡す、偽のブラウザのコマンド（%s に開く場所が入る）。

    webbrowser は、%s を含む指定を shlex（POSIX の決まり）で分けるので、\\ は / に直し（Windows の API は
    どちらも受ける）、空白を含む場所は引用符で包む。background が真なら、終わりに & を付ける（webbrowser は、
    起動してすぐ戻る。付けなければ、コマンドが終わるまで待つ）。"""
    parts = " ".join(shlex.quote(part.replace("\\", "/")) for part in (sys.executable, str(script)))
    return parts + " %s" + (" &" if background else "")


def fake_browser_env(script: Path, background: bool = False) -> dict[str, str]:
    """偽のブラウザ script を BROWSER に置く環境変数。偽が失敗した記録は script と同じ場所の .failed。"""
    return {"BROWSER": browser_command(script, background),
            "CIRCUIT_SIM_TEST_BROWSER_FAILED": str(script.with_suffix(".failed"))}


def launch_with_fake_browser(script: Path, *args: str, config: Path, lang: str = "ja_JP.UTF-8",
                             stdin=subprocess.DEVNULL, background: bool = False,
                             entry: str = BROWSER_GUARD) -> procs.Launched:
    """ブラウザを開く起動（--no-browser 無し）。ブラウザは偽の script だけ（見張りつき。BROWSER_GUARD）。"""
    env = app_env(config, lang, fake_browser_env(script, background))
    env.pop("CIRCUIT_SIM_TEST_BROWSER_OPEN", None)
    return procs.Launched([sys.executable, "-c", entry, *args], cwd=ROOT, env=env, stdin=stdin)


def fake_browser_failure(script: Path) -> str | None:
    """偽のブラウザが失敗していたら、そのとき渡された場所。失敗していなければ None。"""
    failed = script.with_suffix(".failed")
    return failed.read_text(encoding="utf-8") if failed.exists() else None


def read_lines_until_url(proc: procs.Launched, timeout: float = 15) -> tuple[str, list[str]]:
    """起動した場所（URL）の行までを読む。timeout 秒のうちに出なければ、それまでの出力を添えて落とす。"""
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while True:
        try:
            line = proc.next_line(deadline - time.monotonic())
        except AssertionError as exc:
            raise AssertionError(f"起動した場所の表示が無い。{exc}") from None
        lines.append(line)
        m = URL_RE.search(line)
        if m:
            return m.group(0), lines


def next_lines(proc: procs.Launched, count: int, timeout: float = 10.0) -> list[str]:
    """続きの count 行（起動の案内の残りなど）。"""
    deadline = time.monotonic() + timeout
    return [proc.next_line(deadline - time.monotonic()) for _ in range(count)]


class Client:
    """合言葉の URL を一度開いてクッキーを持ち、あとは合言葉の無い URL で API を使う（画面と同じ）。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.base = url.split("?")[0]
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def login(self) -> None:
        with self.opener.open(self.url, timeout=5) as resp:
            assert resp.geturl() == self.base, "合言葉の無い URL へ移っていない"
            assert b'<div id="root">' in resp.read()

    def json(self, path: str, body: dict | None = None, method: str = "GET") -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=5) as resp:
            return json.loads(resp.read())


def wait_up(base: str, timeout: float) -> float:
    """入口のページ（合言葉は要らない）が出るまで待つ。起動の番号は使わない。"""
    started = time.monotonic()
    while True:
        try:
            with urllib.request.urlopen(base, timeout=1) as resp:
                resp.read()
            return time.monotonic() - started
        except OSError:
            if time.monotonic() - started > timeout:
                raise
            time.sleep(0.05)


def wait_ready(client: Client, timeout: float) -> float:
    """受け付けを始めるまで待ってから、起動の URL を1回だけ開いてクッキーを受け取る。"""
    started = time.monotonic()
    wait_up(client.base, timeout)
    client.login()
    client.json("api/health")
    return time.monotonic() - started


def token_of(config: Path) -> str:
    return (config / "token").read_text(encoding="ascii").strip()


def stop(proc: procs.Launched) -> str:
    """止める（子も）。出力の全部を返す。"""
    return proc.stop()


def test_starts_and_serves_page_quickly(tmp_path):
    started = time.monotonic()
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        assert time.monotonic() - started < START_LIMIT_S
        port = int(URL_RE.match(url).group(1))
        # 28470 か、使われていれば 28471〜28479（どれも使われていれば OS が選んだポート）
        assert port in (DEFAULT_PORT, *FALLBACK_PORTS) or port > 0
        assert "?k=" in url and token_of(tmp_path) not in url
    finally:
        stop(proc)


def test_key_is_kept_across_restarts_so_open_pages_reconnect(tmp_path):
    """合言葉は起動のたびに変えない: 前の起動で付いたクッキーで、起動し直したアプリ本体にもつながる。"""
    proc = launch("--no-browser", config=tmp_path)
    try:
        url1, _ = read_lines_until_url(proc)
        client = Client(url1)
        wait_ready(client, START_LIMIT_S)
    finally:
        stop(proc)
    proc = launch("--no-browser", "--port", URL_RE.match(url1).group(1), config=tmp_path)
    try:
        url2, _ = read_lines_until_url(proc)
        assert url2.split("?")[0] == url1.split("?")[0] and url2 != url1  # 同じポート、新しい起動の番号
        deadline = time.monotonic() + START_LIMIT_S
        while True:
            try:
                assert client.json("api/health")["instance"]  # ログインし直さない
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
    finally:
        stop(proc)


def test_api_from_a_plain_client_is_refused(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        wait_ready(Client(url), START_LIMIT_S)
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(url.split("?")[0] + "api/session", timeout=5)
        assert info.value.code == 401
    finally:
        stop(proc)


def test_default_port_falls_back_to_the_next(tmp_path):
    """28470 が使われていれば 28471〜28479 を順に試す（D-050）。"""
    holder = socket.socket()
    if not WINDOWS:  # 前の試験のアプリ本体の TIME_WAIT で、押さえ損ねないように（アプリ本体と同じ設定）
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        holder.bind(("127.0.0.1", DEFAULT_PORT))
        holder.listen(1)
    except OSError:
        holder.close()
        holder = None  # ほかのプログラムが使っている（それでも、次を試すことは確かめられる）
    try:
        proc = launch("--no-browser", config=tmp_path)
        try:
            url, lines = read_lines_until_url(proc)
            port = int(URL_RE.match(url).group(1))
            assert port != DEFAULT_PORT
            assert port in FALLBACK_PORTS or all(_busy(p) for p in FALLBACK_PORTS)
            notice = f"ポート {DEFAULT_PORT} が使えないため、ポート {port} で起動します。"
            assert any(notice in ln for ln in lines), lines
        finally:
            stop(proc)
    finally:
        if holder:
            holder.close()


def _busy(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


@pytest.mark.parametrize(("lang", "expected"), [
    ("ja_JP.UTF-8", "ポート {port} は、ほかのプログラムが使っています"),
    ("en_US.UTF-8", "Port {port} is being used by another program"),
], ids=["ja", "en"])
def test_busy_port_is_reported_and_exits_2(tmp_path, lang, expected):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        code, out = run_to_end("--no-browser", "--port", str(port), config=tmp_path, lang=lang)
    assert code == 2
    assert expected.format(port=port) in out


def fake_binder(failing: dict[int, OSError]):
    tried: list[int] = []

    def binder(port: int):
        tried.append(port)
        if port in failing:
            raise failing[port]
        return f"socket:{port}"

    return binder, tried


def test_scan_order_is_28470_then_28471_to_28479_then_os():
    busy = OSError(errno.EADDRINUSE, "in use")
    binder, tried = fake_binder({p: busy for p in (DEFAULT_PORT, *FALLBACK_PORTS)})
    sock, failures = bind_any(None, binder)
    assert sock == "socket:0"
    assert tried == [28470, 28471, 28472, 28473, 28474, 28475, 28476, 28477, 28478, 28479, 0]
    assert [p for p, _ in failures] == tried[:-1]
    binder, tried = fake_binder({28470: busy, 28471: OSError(errno.EACCES, "denied")})
    assert bind_any(None, binder)[0] == "socket:28472"


def test_previous_port_is_tried_first():
    """前回の起動のポート（28473）を、28470 より先に試す（開いている画面は前回のポートを見ている）。"""
    binder, tried = fake_binder({})
    sock, failures = bind_any(None, binder, previous=(28473, 999999), alive=lambda pid: False,
                              listening=lambda port: False)
    assert sock == "socket:28473" and tried == [28473] and failures == []


def flaky_binder(port: int, busy_times: int):
    """port が busy_times 回だけ使用中、その後は結べる。"""
    tried: list[int] = []

    def binder(p: int):
        tried.append(p)
        if p == port and tried.count(p) <= busy_times:
            raise OSError(errno.EADDRINUSE, "in use")
        return f"socket:{p}"

    return binder, tried


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_previous_port_is_retried_while_only_old_connections_remain():
    """前回のプロセスがもう無く、待ち受けているものも無いのに使用中（OS に前回の接続の後始末が残る）なら、

    同じポートを結び直してみる。上限（5 秒）のうちに結べれば、そのポート。"""
    binder, tried = flaky_binder(28470, 10)
    clock = FakeClock()
    sock, failures = bind_any(None, binder, previous=(28470, 4242), alive=lambda pid: False,
                              listening=lambda port: False, sleep=clock.sleep, clock=clock)
    assert sock == "socket:28470" and failures == []
    assert tried.count(28470) == 11 and 1.9 < clock.now < 2.3


@pytest.mark.parametrize(("alive", "listening", "busy", "why"), [
    (True, False, 1, "前回のプロセスがまだ動いている（ほかの circuit-sim が同じ設定の場所で動いている）"),
    (False, True, 1, "そのポートで、ほかのプログラムが待ち受けている"),
    (False, False, 1000, "上限まで待っても結べない"),
], ids=["previous-process-alive", "another-program-listening", "never-free"])
def test_previous_port_gives_way_when_retrying_cannot_help(alive, listening, busy, why):
    binder, tried = flaky_binder(28470, busy)
    clock = FakeClock()
    sock, failures = bind_any(None, binder, previous=(28470, 4242), alive=lambda pid: alive,
                              listening=lambda port: listening, sleep=clock.sleep, clock=clock)
    assert sock == "socket:28471", why
    assert [p for p, _ in failures] == [28470]
    assert clock.now <= RESTART_WAIT_SECONDS + 0.5
    if alive or listening:
        assert tried.count(28470) == 1 and clock.now == 0.0  # 待たない


def test_explicit_port_is_retried_when_it_was_the_previous_one():
    binder, tried = flaky_binder(8765, 3)
    clock = FakeClock()
    sock, _ = bind_any(8765, binder, previous=(8765, 4242), alive=lambda pid: False,
                       listening=lambda port: False, sleep=clock.sleep, clock=clock)
    assert sock == "socket:8765" and tried == [8765] * 4


def test_pid_alive_tells_running_and_finished_processes_apart():
    assert cli.pid_alive(os.getpid())
    proc = procs.spawn([sys.executable, "-c", "pass"])
    try:
        proc.wait(timeout=20)
    finally:
        procs.kill_tree(proc)
    assert not cli.pid_alive(proc.pid)
    assert not cli.pid_alive(0)


def test_restart_returns_to_the_previous_port_with_a_browser_still_connected(tmp_path):
    """起動し直すと、前回のポートに戻る（--port を付けなくても。開いている画面がそのまま戻れる）。

    前回は 28470 の外の空いているポート P で起動し、ブラウザの代わりに待ちの接続（keep-alive）を3本開いたまま
    止める（POSIX は SIGINT、Windows は Ctrl+Break）。--port 無しで起動し直すと P で起動し、前のクッキーで
    そのまま使える。Windows の実機では local_ci で流れる。"""
    import http.client

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0

    def start(*args: str) -> procs.Launched:
        return launch("--no-browser", *args, config=tmp_path, creationflags=flags)

    proc = start("--port", str(port))
    conns = []
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        cookie = "; ".join(f"{c.name}={c.value}" for c in client.jar)
        for _ in range(3):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/health", headers={"Cookie": cookie})
            conn.getresponse().read()
            conns.append(conn)
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")
        assert proc.wait(timeout=15) == 0  # Ctrl+C でも、Windows の Ctrl+Break でも
        proc = start()  # --port 無し
        url2, lines = read_lines_until_url(proc)
        assert f":{port}/" in url2, ("前回のポートに戻らなかった", lines)
        deadline = time.monotonic() + START_LIMIT_S
        while True:
            try:
                assert client.json("api/health")["instance"]  # 前のクッキーのまま
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
    finally:
        for conn in conns:
            conn.close()
        stop(proc)


def test_explicit_port_is_the_only_one_tried():
    binder, tried = fake_binder({8765: OSError(errno.EADDRINUSE, "in use")})
    sock, failures = bind_any(8765, binder)
    assert sock is None and tried == [8765] and len(failures) == 1


def winerror(code: int) -> OSError:
    """Windows のソケットのエラー code（WSAGetLastError の値）の身代わり。CPython のソースの決まりの形で作る。

    CPython は、ソケットの失敗を OSError(errno, 文, None, winerror) にする（Modules/socketmodule.c の
    set_error → PyErr_SetExcFromWindowsErr）。errno は PC/errmap.h の winerror_to_errno が決める:
    - WSAEACCES（10013）は 10000 を引いた 13（EACCES）。例外の型は PermissionError
    - WSAEADDRINUSE（10048）などは、そのまま 10048（Windows の errno.EADDRINUSE も 10048。
      Modules/errnomodule.c が、errno.h の値ではなく WSA の値を使う）
    winerror の項目は、Windows でない OS の Python では作られないので、後から足す。
    Windows の実物の OSError が同じ組になることは、下の Windows の試験が確かめる
    （test_real_bind_errors_on_windows_have_the_numbers_of_the_stand_ins）。"""
    posix_like = {10004: 4, 10009: 9, 10013: 13, 10014: 14, 10022: 22, 10024: 24}  # winerror_to_errno の表
    exc = OSError(posix_like.get(code, code), "windows")
    exc.winerror = code
    return exc


@pytest.mark.parametrize(("exc", "kind"), [
    (OSError(errno.EADDRINUSE, "in use"), "in_use"),
    (winerror(10048), "in_use"),       # WSAEADDRINUSE（errno も winerror も 10048）
    (OSError(errno.EACCES, "denied"), "denied"),
    (winerror(10013), "denied"),       # WSAEACCES（errno は 13、winerror は 10013）
    (OSError(errno.EADDRNOTAVAIL, "x"), "other"),
    (winerror(10049), "other"),        # WSAEADDRNOTAVAIL
], ids=["posix-in-use", "windows-10048", "posix-denied", "windows-10013", "posix-other", "windows-10049"])
def test_bind_errors_are_told_apart(exc, kind):
    assert port_problem(exc) == kind


def test_stand_in_windows_errors_follow_the_cpython_error_table():
    """身代わりの Windows のエラーが、CPython のソースの決まり（winerror の説明）の組になっている。

    手で写した表との突き合わせで、実物との突き合わせではない（実物は、下の Windows の試験）。"""
    denied, in_use = winerror(10013), winerror(10048)
    assert (denied.errno, denied.winerror) == (13, 10013) and isinstance(denied, PermissionError)
    assert (in_use.errno, in_use.winerror) == (10048, 10048)


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の、結べないときの実物のエラー（POのPCの local_ci で流れる）")
def test_real_bind_errors_on_windows_have_the_numbers_of_the_stand_ins(record_property):
    """Windows で実際に結び損ねたときの OSError の（errno、winerror）が、身代わりと同じ組になる。

    - 使用中: 製品の結び方（何も付けない）で結んだポートを、同じ結び方でもう一度結ぶ → WSAEADDRINUSE
    - 許されない: SO_EXCLUSIVEADDRUSE で結んだポートを、SO_REUSEADDR で結ぶ → WSAEACCES
    （Microsoft の文書 Using SO_REUSEADDR and SO_EXCLUSIVEADDRUSE の表: 1つ目が既定で 2つ目も既定なら
    INUSE、1つ目が SO_EXCLUSIVEADDRUSE で 2つ目が SO_REUSEADDR なら ACCESS）。
    実物の組は、結果のファイル（JUnit XML の properties）に残す。"""
    first = cli.bind(0)
    try:
        with pytest.raises(OSError) as busy:
            cli.bind(first.getsockname()[1]).close()
    finally:
        first.close()
    with socket.socket() as exclusive, socket.socket() as reuse:
        exclusive.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        exclusive.bind(("127.0.0.1", 0))
        reuse.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError) as refused:
            reuse.bind(("127.0.0.1", exclusive.getsockname()[1]))
    seen = {"in_use": (busy.value.errno, busy.value.winerror),
            "denied": (refused.value.errno, refused.value.winerror)}
    record_property("bind_errors", repr(seen))
    assert seen["in_use"] == (winerror(10048).errno, 10048) == (10048, 10048), seen
    assert seen["denied"] == (winerror(10013).errno, 10013) == (13, 10013), seen
    assert isinstance(refused.value, PermissionError)
    assert port_problem(busy.value) == "in_use" and port_problem(refused.value) == "denied"


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_each_bind_error_has_its_own_guidance(lang):
    tr = Texts(lang)
    in_use = bind_failure_text(tr, 9000, [(9000, OSError(errno.EADDRINUSE, "in use"))])
    denied = bind_failure_text(tr, 9000, [(9000, winerror(10013))])
    other = bind_failure_text(tr, 9000, [(9000, OSError(errno.EADDRNOTAVAIL, "not available"))])
    none = bind_failure_text(tr, None, [(0, OSError(errno.EADDRNOTAVAIL, "not available"))])
    assert len({in_use, denied, other, none}) == 4
    assert "9000" in in_use and "9000" in denied and "netsh" in denied and "netstat" in denied
    assert "not available" in other and "not available" in none
    assert ("ポート" if lang == "ja" else "Port") in in_use


@pytest.mark.skipif(WINDOWS or os.geteuid() == 0,
                    reason="POSIX の一般の利用者でだけ、1024 未満のポートが拒まれる")
def test_privileged_port_is_reported_as_denied(tmp_path):
    code, out = run_to_end("--no-browser", "--port", "1", config=tmp_path)
    assert code == 2
    assert "ポート 1 は使えません" in out


def excluded_ranges(netsh_output: str) -> list[tuple[int, int, bool]]:
    """netsh interface ipv4 show excludedportrange の出力を（最初のポート、最後のポート、* の印）にする。

    * の印は「Administered port exclusions」（管理者が netsh add excludedportrange で足した範囲）。
    見出しは Windows の表示言語で変わるので、数字の行だけを読む。"""
    found = []
    for line in netsh_output.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s*(\*?)\s*$", line)
        if m:
            found.append((int(m.group(1)), int(m.group(2)), bool(m.group(3))))
    return found


def test_netsh_excluded_ranges_are_read_with_the_administered_mark():
    """netsh の出力の読み方（多くの Windows に、既定で 50000〜50059 の * つきの範囲がある）。"""
    sample = (
        "\nProtocol tcp Port Exclusion Ranges\n\nStart Port    End Port\n----------    --------\n"
        "      5357        5357\n     49693       49792\n     50000       50059     *\n\n"
        "* - Administered port exclusions.\n\n"
    )
    assert excluded_ranges(sample) == [(5357, 5357, False), (49693, 49792, False), (50000, 50059, True)]
    assert excluded_ranges("プロトコル tcp ポート除外範囲\n\n開始ポート    終了ポート\n") == []


def windows_reserved_ports(limit: int = 6) -> list[tuple[int, bool, OSError | None]]:
    """Windows で OS が予約しているポートの範囲の最初のポートを、製品と同じ結び方で結んでみた結果。

    （ポート、* の印、結べなかった理由。結べたら None）。netsh を呼べなければ空。"""
    try:
        out = subprocess.run(["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                             capture_output=True, text=True, errors="replace", timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    results: list[tuple[int, bool, OSError | None]] = []
    for first, _last, administered in excluded_ranges(out):
        if first < 1024 or len(results) >= limit:
            continue
        try:
            cli.bind(first).close()
            results.append((first, administered, None))
        except OSError as exc:
            results.append((first, administered, exc))
    return results


def shown_bind_result(port: int, administered: bool, exc: OSError | None) -> str:
    mark = "*" if administered else ""
    if exc is None:
        return f"{port}{mark}: 結べた"
    return f"{port}{mark}: errno={exc.errno} winerror={getattr(exc, 'winerror', None)}"


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の、OS が予約しているポートの範囲（POのPCの local_ci で流れる）")
def test_windows_excluded_port_range_is_reported_as_denied(tmp_path, record_property):
    """OS に拒まれる（WSAEACCES 10013）予約のポートは、「使えません」と netsh の確かめ方で案内する。

    netsh に出る範囲のすべてが拒まれるわけではない。* の印の範囲（管理者が足した除外）は、結べる。
    PO の PC の範囲は「50000 50059 *」で、直す前のこの試験は、先頭の 50000 を渡して起動した。起動は成功し、
    試験は止めずに終わったので、アプリ本体が 50000 で待ち受けたまま残った（流し直しで「ほかのプログラムが
    使っています」と出たのは、その残りがポートを持っていたため）。
    だから、拒まれるかどうかは、製品と同じ結び方で実際に結んで確かめてから選び、起動は run_to_end で流す
    （終わらなければ、止めてから落とす）。"""
    probed = windows_reserved_ports()
    shown = "、".join(shown_bind_result(*item) for item in probed) or "予約の範囲が無い"
    record_property("reserved_ports", shown)
    refused = [port for port, _, exc in probed if exc is not None and getattr(exc, "winerror", None) == 10013]
    if not refused:
        pytest.skip(f"この PC には、OS に拒まれる（10013）予約のポートが無い: {shown}")
    port = refused[0]
    # 先の確かめと起動のあいだにポートが空くと、起動に成功する。run_to_end は、そのとき止めてから落とす
    code, out = run_to_end("--no-browser", "--port", str(port), config=tmp_path)
    assert code == 2
    assert f"ポート {port} は使えません" in out and "netsh" in out


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の、OS が予約しているポートの範囲（POのPCの local_ci で流れる）")
def test_windows_reserved_ports_are_reported_as_the_os_answers(tmp_path, record_property):
    """予約の範囲のポートで起動できないとき、案内は OS の答えのとおり。

    OS の答えは、製品と同じ結び方で結んで確かめる: 10013（WSAEACCES）なら「使えません」、10048
    （WSAEADDRINUSE）なら「ほかのプログラムが使っています」。結べたポートは確かめない（起動できてしまう）。
    確かめたポートと OS の答えは、結果のファイル（JUnit XML の properties）に残す。"""
    probed = windows_reserved_ports()
    shown = "、".join(shown_bind_result(*item) for item in probed) or "予約の範囲が無い"
    record_property("reserved_ports", shown)
    failing = [(port, exc) for port, _, exc in probed if exc is not None]
    if not failing:
        pytest.skip(f"この PC には、結べない予約のポートが無い: {shown}")
    for port, exc in failing[:3]:
        expected = {"in_use": f"ポート {port} は、ほかのプログラムが使っています",
                    "denied": f"ポート {port} は使えません",
                    "other": f"ポート {port} で待ち受けられませんでした"}[port_problem(exc)]
        code, out = run_to_end("--no-browser", "--port", str(port), config=tmp_path / str(port))
        assert code == 2, (shown, out)
        assert expected in out, (shown, out)
        code = getattr(exc, "winerror", None)
        assert (code == 10048) == ("ほかのプログラムが使っています" in out), (shown, out)
        assert (code == 10013) == ("使えません" in out and "netsh" in out), (shown, out)


@pytest.mark.parametrize(("lang", "words"), [
    ("ja_JP.UTF-8",
     ["使い方:", "オプション", "待ち受けるポート", "ブラウザを開かない", "この説明を出して終わる"]),
    ("en_US.UTF-8", ["usage:", "options", "port to listen on", "don't open a browser", "show this help"]),
], ids=["ja", "en"])
def test_help_is_in_one_language(tmp_path, lang, words):
    code, out = run_to_end("--no-browser", "--help", config=tmp_path, lang=lang)
    assert code == 0
    for word in words:
        assert word in out, (word, out)
    if lang.startswith("en"):
        assert not re.search(r"[぀-ヿ一-鿿]", out), out
    else:
        assert "show this help" not in out and "usage:" not in out


@pytest.mark.parametrize(("args", "lang", "text"), [
    (["--port", "70000"], "ja_JP.UTF-8",
     "circuit-sim: エラー: --port には 0〜65535 のポートを指定してください（指定: 70000）。"),
    (["--port", "abc"], "en_US.UTF-8",
     "circuit-sim: error: --port must be a port from 0 to 65535 (given: abc)."),
    (["--bogus"], "ja_JP.UTF-8",
     "circuit-sim: エラー: 知らないオプションです: --bogus。"
     "使えるオプションは circuit-sim --help で確かめてください。"),
    (["--bogus"], "en_US.UTF-8",
     "circuit-sim: error: Unknown option: --bogus. See circuit-sim --help for the options."),
    # ファイルを渡したとき（UX 確認3回目の改善4）
    (["examples/rlc_series.json"], "ja_JP.UTF-8",
     "circuit-sim: エラー: circuit-sim は、起動するときにファイルを受け取りません"
     "（examples/rlc_series.json）。"
     "仕様JSONは、起動した後に画面の「開く」で開いてください。"),
    (["spec.json"], "en_US.UTF-8",
     "circuit-sim: error: circuit-sim doesn't take a file when it starts (spec.json). "
     "Open the spec JSON with Open in the workspace after it starts."),
    (["-x"], "ja_JP.UTF-8", "知らないオプションです: -x。"),
    (["--port"], "ja_JP.UTF-8", "circuit-sim: エラー: --port の後ろに値を指定してください。"),
    (["--port"], "en_US.UTF-8", "circuit-sim: error: --port needs a value."),
    (["--no-browser=yes"], "ja_JP.UTF-8", "circuit-sim: エラー: --no-browser には値を付けられません。"),
    (["--no-browser=yes"], "en_US.UTF-8", "circuit-sim: error: --no-browser doesn't take a value."),
], ids=["port-range-ja", "port-not-a-number-en", "unknown-option-ja", "unknown-option-en", "file-ja",
        "file-en", "short-option-ja", "port-without-value-ja", "port-without-value-en",
        "flag-with-value-ja", "flag-with-value-en"])
def test_bad_arguments_are_explained_in_one_language(tmp_path, args, lang, text):
    """引数の誤りの行に、argparse の英語（argument --port:、unrecognized arguments など）を残さない。

    （UX の改善5）。どの起動にも、頭に --no-browser を付ける（誤った引数で起動してしまう後退が入っても、
    本物のブラウザが開かないように。誤りの見分けと文は、付けても変わらない）。"""
    code, out = run_to_end("--no-browser", *args, config=tmp_path, lang=lang)
    assert code == 2 and text in out, out
    for english in ("argument ", "unrecognized", "expected one", "ignored explicit", "invalid"):
        assert english not in out, out
    if lang.startswith("ja"):
        assert "使い方:" in out and "usage:" not in out


def test_parser_falls_back_to_the_table_for_other_errors(capsys):
    """argparse が自分で error を呼ぶ誤り（見分けられないもの）も、言語の表の文で出す。"""
    parser = cli.Parser(Texts("ja"))
    with pytest.raises(SystemExit) as info:
        parser.error("something odd")
    assert info.value.code == 2
    assert "circuit-sim: エラー: 引数が正しくありません（something odd）。" in capsys.readouterr().err


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_failures_tell_how_to_fix(lang):
    """設定の場所に書けない、待ち受けるポートを用意できないときは、どう直すかも出す（UX の改善4）。"""
    tr = Texts(lang)
    config = tr("config.failed", path="/x", reason="Not a directory")
    none = bind_failure_text(tr, None, [(0, OSError(errno.EADDRNOTAVAIL, "not available"))])
    if lang == "ja":
        assert "フォルダの権限、空き容量、同じ名前のファイルが無いか" in config
        assert "セキュリティソフトなどが、このPCの中の通信（127.0.0.1）を止めていないか" in none
    else:
        assert "permissions, free space, no file with the same name" in config
        assert "security software isn't blocking connections within this computer (127.0.0.1)" in none


@pytest.mark.parametrize("lang", ["ja", "en"])
def test_access_denied_mentions_both_causes(lang):
    """WSAEACCES（10013）は、ほかのプログラムがポートを専用で使っているときにも出る。予約の範囲と決めつけない。"""
    text = bind_failure_text(Texts(lang), 9000, [(9000, winerror(10013))])
    assert "netstat -ano" in text and "netsh interface ipv4 show excludedportrange" in text
    other = {"ja": "ほかのプログラムがこのポートを専用で使っている",
             "en": "Another program may be using it"}[lang]
    assert other in text


@pytest.mark.parametrize(("env", "windows", "lang"), [
    ({"LC_ALL": "ja_JP.UTF-8"}, False, "ja"),
    ({"LANG": "ja_JP.eucJP"}, False, "ja"),
    ({"LC_ALL": "C", "LANG": "ja_JP.UTF-8"}, False, "en"),   # LC_ALL が先
    ({"LC_MESSAGES": "ja_JP", "LANG": "en_US"}, False, "ja"),
    ({"LANG": "en_US.UTF-8"}, False, "en"),
    ({}, False, "en"),
    ({"LANG": "ja"}, True, "ja"),
])
def test_terminal_language_follows_the_os_setting(env, windows, lang):
    assert terminal_lang(env, windows=windows) == lang


class FakeWindll:
    """ctypes.windll の代わり。GetUserDefaultUILanguage が、決めた言語の番号（LANGID）を返す。"""

    def __init__(self, langid: int) -> None:
        self.kernel32 = self
        self.langid = langid

    def GetUserDefaultUILanguage(self) -> int:  # Windows の API の名前のまま
        return self.langid


@pytest.mark.parametrize(("langid", "lang"), [
    (0x0411, "ja"),  # ja-JP
    (0x0409, "en"),  # en-US
    (0x0809, "en"),  # en-GB
    (0x0804, "en"),  # zh-CN（日本語でなければ英語）
], ids=["ja-JP", "en-US", "en-GB", "zh-CN"])
def test_windows_display_language_decides_when_no_variable_is_set(monkeypatch, langid, lang):
    """言語の環境変数が無い Windows では、表示言語（GetUserDefaultUILanguage）で決める。

    この分かれ目は、Windows の API を差し替えて流す（どの OS でも）。番号は Windows の言語の番号（LANGID）:
    下位 10 ビットが主言語で、日本語は 0x11。ja-JP は 0x0411、en-US は 0x0409（Microsoft の文書
    Language Identifier Constants and Strings と MAKELANGID）。実物の呼び出しは、下の Windows の試験。"""
    import ctypes

    monkeypatch.setattr(ctypes, "windll", FakeWindll(langid), raising=False)
    assert terminal_lang({}, windows=True) == lang
    assert terminal_lang({"LANG": "en_US.UTF-8"}, windows=True) == "en"  # 環境変数が先


def test_windows_display_language_falls_back_to_english_when_the_api_is_missing(monkeypatch):
    import ctypes

    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert terminal_lang({}, windows=True) == "en"


@pytest.mark.skipif(not WINDOWS, reason="Windows の表示言語（POのPCの local_ci で流れる）")
def test_terminal_language_on_windows_follows_the_display_language(tmp_path):
    """言語の環境変数が無い Windows の起動で、端末の文言が OS の表示言語になる（実物の呼び出し）。"""
    import ctypes

    langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    expected = "ja" if langid & 0x3FF == 0x11 else "en"
    assert terminal_lang({}, windows=True) == expected
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("LC_ALL", "LC_MESSAGES", "LANG")}
    env.update(PYTHONIOENCODING="utf-8", CIRCUIT_SIM_CONFIG_DIR=str(tmp_path))
    done = procs.Launched([sys.executable, "-m", "circuit_sim.app", "--no-browser", "--help"], cwd=ROOT,
                          env=env)
    try:
        code, out = done.end(60)
    finally:
        done.stop()
    assert code == 0, out
    assert ("使い方:" in out) == (expected == "ja"), (hex(langid), out)
    assert ("usage:" in out) == (expected == "en"), (hex(langid), out)


def test_terminal_texts_exist_in_both_languages():
    for key, (ja, en) in MESSAGES.items():
        assert ja and en, key
        assert sorted(re.findall(r"\{(\w+)\}", ja)) == sorted(re.findall(r"\{(\w+)\}", en)), key
        assert not re.search(r"[぀-ヿ一-鿿]", en), key
        assert "番号" not in ja, f"{key}: 「番号」ではなく「ポート」と書く（--port と同じ言葉）"


def test_there_is_no_switch_to_skip_the_key():
    """製品に、合言葉を確かめない抜け道（環境変数など）が無い。"""
    source = Path(cli.__file__).read_text(encoding="utf-8") + Path(cli.__file__).with_name(
        "server.py").read_text(encoding="utf-8")
    assert "NO_TOKEN" not in source and "PYTEST" not in source
    with pytest.raises(ValueError):
        create_app(token="", settings=Settings(ROOT / "unused"))


def recording_browser(folder: Path, before: str = "") -> tuple[Path, Path]:
    """偽のブラウザ（渡された引数を JSON で控えて終わる。画面は開かない）と、控えのファイル。

    before は、控える前に実行する文（待たせるときなど）。控えは、書き終えてから名前を付ける（読む側が、
    書きかけを読まない）。"""
    record, script = folder / "opened.json", folder / "fake_browser.py"
    script.write_text(
        "import json, os, sys, time\n"
        f"{before}\n"
        f"record = {str(record)!r}\n"
        "with open(record + '.part', 'w', encoding='utf-8') as f:\n"
        "    json.dump(sys.argv, f)\n"
        "os.replace(record + '.part', record)\n",
        encoding="utf-8",
    )
    return script, record


def wait_for_browser_record(record: Path, script: Path, proc: procs.Launched, timeout: float) -> list[str]:
    """偽のブラウザが控えた引数。呼ばれなければ、偽のブラウザが失敗していたかどうかを添えて落とす。"""
    deadline = time.monotonic() + timeout
    while not record.exists():
        if time.monotonic() > deadline:
            failed = fake_browser_failure(script)
            raise AssertionError(
                "偽のブラウザが呼ばれない（ブラウザのコマンドが始まっていない）" if failed is None
                else f"偽のブラウザが失敗した（渡された場所: {failed.strip()}）"
            )
        assert proc.poll() is None, "アプリ本体が終わった: " + proc.out
        time.sleep(0.05)
    return json.loads(record.read_text(encoding="utf-8"))


@pytest.mark.parametrize("stdin", [subprocess.DEVNULL, subprocess.PIPE], ids=["stdin-null", "stdin-pipe"])
def test_browser_gets_only_the_location_of_a_private_page(tmp_path, stdin):
    """既定のブラウザ（BROWSER の指定で、偽のブラウザに差し替える）に渡すのは、転送用の HTML の場所だけ。

    ブラウザの起動の引数（ほかの利用者にも /proc/<pid>/cmdline で見える）には、起動の番号も合言葉も出ない。
    HTML は本人だけが読める（POSIX は 0600、場所は 0700。Windows の権限は test_app_config.py）。中身は端末に
    出した URL へ移るだけ。番号が使われたら消える。

    stdin-pipe は、アプリ本体が標準入力のパイプから Enter を読む起動（ほかのプログラムから起動された形）。
    Windows では、アプリ本体がパイプを読んで待っていると、その間に起動されたブラウザのコマンドが始まれない
    （PO の PC で、受け入れ試験の偽のブラウザが 30 秒たっても呼ばれなかった。仕組みも実機で確かめた）。
    製品は、Windows のパイプを待たない読み方にしたので、どちらの入力でも START_LIMIT_S のうちに呼ばれるはず
    （Windows でも流れる。直した後の Windows での結果は、まだ無い。Linux では、直す前から通る）。"""
    script, record = recording_browser(tmp_path)
    config = tmp_path / "config"
    proc = launch_with_fake_browser(script, config=config, stdin=stdin)
    try:
        url, lines = read_lines_until_url(proc)
        argv = wait_for_browser_record(record, script, proc, START_LIMIT_S)
        assert fake_browser_failure(script) is None
        token, number = token_of(config), url.split("?k=")[1]
        assert len(argv) == 2, argv  # 偽のブラウザのファイル名と、開く場所だけ
        assert argv[-1].startswith("file:///") and argv[-1].endswith(".html"), argv
        assert not any(token in arg or number in arg for arg in argv), argv
        page = Path(urllib.request.url2pathname(urllib.parse.urlparse(argv[-1]).path))
        assert page.parent.resolve() == config.resolve()
        if not WINDOWS:
            assert stat.S_IMODE(page.stat().st_mode) == 0o600
            assert stat.S_IMODE(config.stat().st_mode) == 0o700
        assert url in page.read_text(encoding="utf-8")
        wait_up(url.split("?")[0], START_LIMIT_S)
        lines += next_lines(proc, 3)
        assert not any(token in line for line in lines), lines
        # ブラウザが HTML から移ったのと同じに、番号を1回使う。HTML は消える。2回目は使えない
        first, second = Client(url), Client(url)
        first.login()
        assert first.json("api/health")["instance"]
        deadline = time.monotonic() + 2
        while page.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not page.exists(), "番号を使った後も、転送用の HTML が残っている"
        second.login()
        with pytest.raises(urllib.error.HTTPError) as info:
            second.json("api/health")
        assert info.value.code == 401
    finally:
        stop(proc)
    assert not list(config.glob("open-*.html"))


class FakeServer:
    started = True
    should_exit = False


def test_open_when_ready_passes_a_file_location_and_tells_when_nothing_opened(tmp_path, monkeypatch):
    """ブラウザに渡すのは file:/// の形の場所（Windows では file:///C:/…）。番号も合言葉も含まない。

    開いたのに番号が使われないまま（HTML がブラウザ以外で開いた、ブラウザがその場所を読めない）なら、
    端末で URL を開くよう案内する。番号が使われたら HTML は消える。Windows でも流れる。"""
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda target: opened.append(target) or True)
    pages = cli.LaunchPages(tmp_path / "config")
    launches = Launches(on_spent=pages.spent)
    number = launches.issue()
    url = cli.launch_url("http://127.0.0.1:28470/", number)
    page = pages.write(number, url, "dark")
    said: list[str] = []
    cli.open_when_ready(FakeServer(), page.as_uri(), url, said.append, Texts("ja"),
                        lambda: not launches.is_open(number), wait=0.3)
    assert opened == [page.as_uri()]
    assert opened[0].startswith("file:///") and number not in opened[0]
    if WINDOWS:
        assert re.match(r"file:///[A-Za-z]:/", opened[0]), opened
    else:
        assert stat.S_IMODE(page.stat().st_mode) == 0o600
    text = page.read_text(encoding="utf-8")
    assert f"location.replace({json.dumps(url)})" in text and 'content="dark"' in text
    assert said == [f"ブラウザで作業画面が開かないときは、ブラウザで次の場所を開いてください: {url}"]
    assert launches.redeem(number)
    assert not page.exists()


def test_open_when_ready_is_quiet_once_the_number_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.webbrowser, "open", lambda target: True)
    said: list[str] = []
    cli.open_when_ready(FakeServer(), "file:///x.html", "http://u", said.append, Texts("en"), lambda: True,
                        wait=0.3)
    assert said == []
    monkeypatch.setattr(cli.webbrowser, "open", lambda target: False)
    cli.open_when_ready(FakeServer(), "file:///x.html", "http://u", said.append, Texts("en"), lambda: False,
                        wait=0.3)
    assert said == ["Couldn't open a browser. Open this address in your browser: http://u"]


@pytest.mark.parametrize("started", [False, True], ids=["before-it-started", "after-it-started"])
def test_open_when_ready_does_not_open_a_browser_while_the_app_is_stopping(monkeypatch, started):
    """止めている最中は、ブラウザを開かない。案内も出さない。

    受け付けを始める前に止められたとき（起動の直後の Ctrl+C）も、始めた直後に止められたときも。
    止めた後に開くと、もう応えないアプリ本体の画面が、ブラウザに開く。"""
    class Stopping:
        should_exit = True

    Stopping.started = started
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda target: opened.append(target) or True)
    said: list[str] = []
    cli.open_when_ready(Stopping(), "file:///x.html", "http://u", said.append, Texts("en"), lambda: False,
                        wait=0.3)
    assert opened == [], "止めている最中に、ブラウザを開いた"
    assert said == []


def test_open_when_ready_gives_no_guidance_once_the_app_is_stopping(monkeypatch):
    """ブラウザを開いた後、番号が使われるのを待つ間に止められたら、案内を出さずに、すぐ戻る。"""
    server = FakeServer()

    def opened_then_stopped(target) -> bool:
        server.should_exit = True  # 開いた直後に、止められた
        return True

    monkeypatch.setattr(cli.webbrowser, "open", opened_then_stopped)
    said: list[str] = []
    began = time.monotonic()
    cli.open_when_ready(server, "file:///x.html", "http://u", said.append, Texts("en"), lambda: False,
                        wait=30)
    assert said == [], "止めた後に、ブラウザの案内を出した"
    assert time.monotonic() - began < 10, "止められた後も、番号が使われるのを待ち続けた"


@pytest.mark.skipif(WINDOWS, reason="POSIX の SIGTERM（Windows は手で窓を閉じて確かめる）")
def test_unused_launch_page_is_removed_when_stopped_by_sigterm(tmp_path):
    """ブラウザが番号を使わないまま（偽のブラウザは開かない）、SIGTERM で止められても、

    転送用の HTML は消える。"""
    script, record = recording_browser(tmp_path)
    config = tmp_path / "config"
    proc = launch_with_fake_browser(script, config=config)
    try:
        url, _ = read_lines_until_url(proc)
        wait_up(url.split("?")[0], START_LIMIT_S)
        wait_for_browser_record(record, script, proc, START_LIMIT_S)
        assert list(config.glob("open-*.html"))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        assert not list(config.glob("open-*.html"))
    finally:
        stop(proc)
    assert fake_browser_failure(script) is None


# ------------------------------------------------------------------ 端末の Enter と、ブラウザのコマンド

# 終わるまで戻らないブラウザの身代わり: 呼ばれたことを控え（started）、止める印（stop）が置かれるまで動く
# （置かれなくても 30 秒で自分で終わる。試験が落ちても、残り続けない）。標準入力は読まない
SLOW_BROWSER = """\
import os, sys, time

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "started.part"), "w", encoding="utf-8") as f:
    f.write(sys.argv[-1])
os.replace(os.path.join(here, "started.part"), os.path.join(here, "started"))
deadline = time.monotonic() + 30
while time.monotonic() < deadline and not os.path.exists(os.path.join(here, "stop")):
    time.sleep(0.05)
with open(os.path.join(here, "done"), "w", encoding="utf-8") as f:
    f.write("done")
"""


@pytest.mark.parametrize("background", [False, True], ids=["waits-for-the-command", "returns-at-once"])
def test_enter_gives_a_new_url_while_the_browser_command_is_running(tmp_path, background):
    """ブラウザのコマンドが動いている間も、端末の Enter で新しい URL が出る。コマンドは、ちゃんと始まる。

    BROWSER のコマンドは、Python の webbrowser が終わりを待つ形（waits-for-the-command。名前を知らない
    コマンド）と、起動してすぐ戻る形（returns-at-once。終わりに & を付けた指定）の両方。
    - Enter は、ブラウザを開く呼び出しが戻るのを待たずに読む（待つ形でも、ブラウザを閉じる前に効く）。
    - コマンドは、アプリ本体が標準入力（パイプ）の Enter を待っている最中に起動されても、始まる。
      Windows では、パイプを読んで待っていると始まれないので、待たない読み方をしている（すぐ戻る形は、
      「開く呼び出しが戻ってから読み始める」やり方では守れない見込みだった。読み始めた後に、コマンドの
      起動が続くため）。"""
    script = tmp_path / "slow_browser.py"
    script.write_text(SLOW_BROWSER, encoding="utf-8")
    proc = launch_with_fake_browser(script, config=tmp_path / "config", stdin=subprocess.PIPE,
                                    background=background)
    try:
        url, _ = read_lines_until_url(proc)
        deadline = time.monotonic() + START_LIMIT_S + 5
        while not (tmp_path / "started").exists():
            assert time.monotonic() < deadline, "ブラウザのコマンドが始まらない: " + proc.out
            assert proc.poll() is None, "アプリ本体が終わった: " + proc.out
            time.sleep(0.05)
        proc.stdin.write("\n")
        proc.stdin.flush()
        line = proc.wait_line("新しい URL", timeout=5)
        assert not (tmp_path / "done").exists(), "ブラウザのコマンドが終わってから、新しい URL が出た"
        assert URL_RE.search(line).group(0) != url
    finally:
        (tmp_path / "stop").write_text("stop", encoding="utf-8")
        stop(proc)
    assert fake_browser_failure(script) is None


# 転送用の HTML を書けない起動の入口（製品のコードは変えない。起動の前に2つ差し替える）:
# - LaunchPages.write を、書けなかったとき（空きが無い）と同じ OSError にする
# - webbrowser.open を、呼ばれたら控えるだけのものにする（呼ばれないはず。呼ばれても、本物は開かない）
NO_LAUNCH_PAGE_ENTRY = """\
import errno, os, sys

from circuit_sim.app import cli


def cannot_write(self, number, url, theme=None):
    raise OSError(errno.ENOSPC, "No space left on device")


def must_not_open(target, *args, **kwargs):
    with open(os.environ["CIRCUIT_SIM_TEST_BROWSER_FAILED"], "a", encoding="utf-8") as f:
        f.write("opened " + str(target) + "\\n")
    return True


cli.LaunchPages.write = cannot_write
cli.webbrowser.open = must_not_open
raise SystemExit(cli.main(sys.argv[1:]))
"""


def test_enter_works_when_the_launch_page_cannot_be_written(tmp_path):
    """転送用の HTML を書けなかった起動でも、URL を案内し、端末の Enter で新しい URL を出す。

    ブラウザは開かない（開く場所が無い）。「ブラウザを開けませんでした」と、端末で開く URL を出す。"""
    script, _record = recording_browser(tmp_path)
    proc = launch_with_fake_browser(script, config=tmp_path / "config", stdin=subprocess.PIPE,
                                    entry=NO_LAUNCH_PAGE_ENTRY)
    try:
        url, _ = read_lines_until_url(proc)
        failed = proc.wait_line("ブラウザを開けませんでした", timeout=START_LIMIT_S)
        assert url in failed
        wait_up(url.split("?")[0], START_LIMIT_S)
        proc.stdin.write("\n")
        proc.stdin.flush()
        line = proc.wait_line("新しい URL", timeout=5)
        assert URL_RE.search(line).group(0) != url
    finally:
        stop(proc)
    assert fake_browser_failure(script) is None, "転送用の HTML が無いのに、ブラウザを開こうとした"


@pytest.mark.parametrize(("code", "failed"), [(0, False), (1, True)], ids=["fake-ok", "fake-fails"])
def test_fake_browser_guard_never_falls_through_to_the_real_browser(tmp_path, code, failed):
    """試験の見張り（BROWSER_GUARD）: 偽のブラウザが失敗しても、webbrowser.open はそこで終わる。

    見張りが無いと、webbrowser は偽の次に OS の既定のブラウザ（本物）を試す。見張りは、偽の結果がどうでも
    「開いた」と答えて止め、失敗は控えに残す。"""
    script = tmp_path / "fake_browser.py"
    script.write_text(f"import sys\nsys.exit({code})\n", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **fake_browser_env(script),
           "CIRCUIT_SIM_TEST_BROWSER_OPEN": "file:///circuit-sim-test/never-opened.html"}
    done = procs.Launched([sys.executable, "-c", BROWSER_GUARD], cwd=ROOT, env=env)
    try:
        returncode, out = done.end(60)
    finally:
        done.stop()
    assert returncode == 0, out
    assert out.strip() == "True"  # 次の候補（OS の既定のブラウザ）へ進まない
    if failed:
        assert fake_browser_failure(script) == "file:///circuit-sim-test/never-opened.html\n"
    else:
        assert fake_browser_failure(script) is None


# ------------------------------------------------------------ Enter の読み方（待つ読み方、待たない読み方）


def posix_pipe_ready(fd: int):
    """Windows の PeekNamedPipe の身代わり（POSIX の本物のパイプで、待たない読み方の道を流すため）。

    届いているバイト数（FIONREAD）。届いていなくて、書く側が閉じていれば None。windows_pipe_ready が作る
    関数と同じ約束。

    量を見てから「閉じたか」（POLLHUP）を見るまでの間に、書く側が「書いて、閉じる」ことがある。閉じたと
    分かった後で、量を読み直す（閉じた後は増えない）。読み直さないと、届いた分が残っているのに None
    （入力の終わり）と答える（レビュー 6回目の軽微1）。"""
    import array
    import fcntl
    import select
    import termios

    def arrived() -> int:
        count = array.array("i", [0])
        fcntl.ioctl(fd, termios.FIONREAD, count)
        return count[0]

    def ready() -> int | None:
        count = arrived()
        if count:
            return count
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        if any(event & select.POLLHUP for _, event in poller.poll(0)):
            return arrived() or None
        return 0

    return ready


def pipe_reader(fd: int):
    """この OS で、パイプ fd を待たない読み方で読むための ready。

    Windows は None（製品が、本物の PeekNamedPipe を使う）。ほかの OS は身代わり。"""
    return None if WINDOWS else posix_pipe_ready(fd)


def watch_read_sizes(monkeypatch, fd: int) -> list[int]:
    """os.read を見張る: 製品が fd を読みに行くたびに、頼んだ大きさを控える（読み取りそのものは本物）。"""
    real_read = os.read
    sizes: list[int] = []

    def watched_read(target, size):
        if target == fd:
            sizes.append(size)
        return real_read(target, size)

    monkeypatch.setattr(cli.os, "read", watched_read)
    return sizes


@pytest.mark.skipif(WINDOWS, reason="身代わり（POSIX の FIONREAD と POLLHUP）の確かめ。Windows は本物を使う")
def test_stand_in_answers_what_is_left_when_the_writer_wrote_and_closed_in_between(monkeypatch):
    """身代わり posix_pipe_ready: 量を見た後、「閉じたか」を見る前に、書く側が「書いて、閉じた」とき。

    届いた分が残っているので、その量を答える（None ではない。None と答えると、製品の読むスレッドは、
    届いていた Enter を読まずに終わる）。読み終えた後は None。
    競合を、決まった順で起こす: 身代わりが「閉じたか」を調べ始める所（select.poll を作る所）で、
    書いて閉じる。"""
    import select

    r, w = os.pipe()
    real_poll = select.poll
    wrote: list[int] = []

    def poll_after_a_write_and_a_close():
        if not wrote:
            wrote.append(os.write(w, b"\n"))
            os.close(w)
        return real_poll()

    try:
        ready = posix_pipe_ready(r)
        assert ready() == 0  # 開いていて、届いていない
        monkeypatch.setattr(select, "poll", poll_after_a_write_and_a_close)
        assert ready() == 1, "届いた分が残っているのに、入力の終わりと答えた"
        assert wrote == [1]
        assert os.read(r, 10) == b"\n"
        assert ready() is None  # 読み終えて、書く側は閉じている
    finally:
        if not wrote:
            os.close(w)
        os.close(r)


def test_watch_enter_reads_a_pipe_and_ends_when_the_writer_closes():
    """パイプの Enter を読む。書く側が閉じたら（入力の終わり）、読むスレッドは終わる。

    Windows では、製品の待たない読み方（本物の PeekNamedPipe）で流れる。ほかの OS は、待つ読み方。"""
    r, w = os.pipe()
    count = []
    got = threading.Event()

    def on_enter() -> None:
        count.append(1)
        got.set()

    try:
        thread = cli.watch_enter(on_enter, "pipe", fd=r)
        assert not got.wait(0.3), "何も送っていないのに、Enter を受けた"
        os.write(w, b"abc")  # Enter でない文字は数えない
        assert not got.wait(0.3)
        os.write(w, b"\n")
        assert got.wait(5), "送った Enter を読まない"
        got.clear()
        os.write(w, b"\r\n")
        assert got.wait(5)
    finally:
        os.close(w)  # 入力の終わり
    thread.join(timeout=5)
    os.close(r)
    assert not thread.is_alive(), "書く側が閉じても、読むスレッドが終わらない"
    assert len(count) == 2


def test_polling_reader_reads_only_what_has_arrived_and_never_waits(monkeypatch):
    """待たない読み方: 届いている量を調べ、届いた分だけ読む。届いていない間は、読みに行かない。

    本物のパイプで流す（Windows は本物の PeekNamedPipe、ほかの OS は身代わりの ready）。os.read を見張り、
    読みに行くたびに、その時点で届いていた量を控える: 0 のときに読みに行けば、そこで待ってしまう。"""
    r, w = os.pipe()
    arrived = pipe_reader(r) or cli.windows_pipe_ready(r)
    real_read = os.read
    reads: list[tuple[int, int]] = []

    def watched_read(fd, size):
        if fd == r:
            reads.append((arrived(), size))
        return real_read(fd, size)

    monkeypatch.setattr(cli.os, "read", watched_read)
    got = threading.Event()
    stop_reading = threading.Event()
    try:
        thread = cli.watch_enter(got.set, "pipe", fd=r, stop=stop_reading, ready=pipe_reader(r))
        time.sleep(0.4)  # 何も届かない間
        assert reads == [], "届いていないのに、読みに行った（そこで待つ）"
        os.write(w, b"x" * 3000 + b"\n")  # 1回で読む量（1024）より多い
        assert got.wait(5)
        time.sleep(0.3)
        assert reads and all(0 < size <= available for available, size in reads), reads
        assert sum(size for _, size in reads) == 3001
    finally:
        stop_reading.set()
        thread.join(timeout=5)
        stopped = not thread.is_alive()  # パイプを閉じる前に見る（閉じると、入力の終わりでも終わる）
        os.close(w)
        os.close(r)
    assert stopped, "止める合図で、読むスレッドが終わらない"


def test_polling_reader_ends_when_the_writer_closes():
    """待たない読み方: 書く側が閉じたら（入力の終わり）、止める合図が無くても、読むスレッドは終わる。

    先に届いていた分は、読んでから終わる。本物のパイプで流す（Windows は本物の PeekNamedPipe）。"""
    r, w = os.pipe()
    got = threading.Event()
    try:
        thread = cli.watch_enter(got.set, "pipe", fd=r, ready=pipe_reader(r))
        time.sleep(0.2)
        assert thread.is_alive()
        os.write(w, b"\n")
    finally:
        os.close(w)
    thread.join(timeout=5)
    ended = not thread.is_alive()
    os.close(r)
    assert ended, "書く側が閉じても、読むスレッドが終わらない"
    assert got.is_set(), "閉じる前に届いていた Enter を、読まずに終わった"


def test_polling_reader_follows_what_the_check_answers():
    """待たない読み方の分かれ目を、調べた結果を差し替えて流す（どの OS でも）。

    答えは windows_pipe_ready が作る関数の約束どおり: 0（届いていない）、正の数（届いている量）、
    None（書く側が閉じた）、OSError（パイプとして調べられない）。
    - 0 の間は読まず、PIPE_POLL_SECONDS おきに調べ直すだけ（回り続けない）
    - 正の数なら、その分だけ読む
    - None なら、読むのをやめる
    - OSError なら、待つ読み方に戻す（パイプでない入力でも、Enter は読める）"""
    r, w = os.pipe()
    answers: list = [0, 0, 0, 2, 0, None]
    asked: list[float] = []

    def scripted():
        asked.append(time.monotonic())
        return answers.pop(0) if answers else 0  # 終わりの後も聞かれたら、届いていないと答え続ける

    count = []
    try:
        os.write(w, b"\n\n")
        thread = cli.watch_enter(lambda: count.append(1), "pipe", fd=r, ready=scripted)
        thread.join(timeout=5)
        assert not thread.is_alive(), "None（書く側が閉じた）で終わらない"
        assert len(asked) == 6, "None（書く側が閉じた）の後も、調べ続けた"
        assert len(count) == 1  # 1回の読み取りに Enter が2つ入っていても、1回
        gaps = [b - a for a, b in zip(asked, asked[1:], strict=False)]
        assert gaps[0] >= cli.PIPE_POLL_SECONDS * 0.8 and gaps[1] >= cli.PIPE_POLL_SECONDS * 0.8, gaps

        def cannot_check():
            raise OSError(errno.EINVAL, "PeekNamedPipe failed (Windows error 1)")

        got = threading.Event()
        thread = cli.watch_enter(got.set, "pipe", fd=r, ready=cannot_check)
        os.write(w, b"\n")
        assert got.wait(5), "調べられない入力で、待つ読み方に戻らない"
    finally:
        os.close(w)
        thread.join(timeout=5)
        os.close(r)
    assert not thread.is_alive()


def test_polling_reader_does_not_spin_while_nothing_arrives():
    """何も届かない間、待たない読み方は CPU を使い続けない。

    2 秒のあいだ何も送らず、調べた回数と、このプロセスが使った CPU の時間を見る。調べるのは
    PIPE_POLL_SECONDS おき（2 秒で 40 回ほど）。CPU の時間の上限 0.5 秒は、回り続けたとき（2 秒近くになる）と
    見分けるための大きめの値。Windows では、本物の PeekNamedPipe で流れる。"""
    r, w = os.pipe()
    inner = pipe_reader(r) or cli.windows_pipe_ready(r)
    asked = []

    def counted():
        asked.append(1)
        return inner()

    stop_reading = threading.Event()
    try:
        thread = cli.watch_enter(lambda: None, "pipe", fd=r, stop=stop_reading, ready=counted)
        used = time.process_time()
        time.sleep(2.0)
        used = time.process_time() - used
        checks = len(asked)
    finally:
        stop_reading.set()
        thread.join(timeout=5)
        stopped = not thread.is_alive()
        os.close(w)
        os.close(r)
    assert stopped, "止める合図で、読むスレッドが終わらない"
    assert 5 <= checks <= 2.0 / cli.PIPE_POLL_SECONDS + 5, checks
    assert used < 0.5, f"何も届かない 2 秒の間に、CPU を {used:.2f} 秒使った（調べた回数 {checks}）"


def test_on_a_windows_pipe_the_product_picks_the_polling_reader_by_itself(monkeypatch):
    """ready を渡さなくても、Windows のパイプなら、製品が自分で待たない読み方を選ぶ（8.3 の対策の中心）。

    製品の Windows の分かれ目（cli.WINDOWS）を真にして流す。調べる関数を作る所（cli.windows_pipe_ready）は、
    Windows では本物、ほかの OS では身代わり（posix_pipe_ready）。何も届かない間は、読みに行かない
    （待つ読み方なら、届いていなくても読みに行って、そこで待つ）。届いたら、届いた分だけ読む。"""
    make = cli.windows_pipe_ready if WINDOWS else posix_pipe_ready
    made: list[int] = []

    def recorded(fd: int):
        made.append(fd)
        return make(fd)

    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "windows_pipe_ready", recorded)
    r, w = os.pipe()
    sizes = watch_read_sizes(monkeypatch, r)
    got = threading.Event()
    stop_reading = threading.Event()
    try:
        thread = cli.watch_enter(got.set, "pipe", fd=r, stop=stop_reading)
        time.sleep(0.4)  # 何も届かない間
        assert made == [r], "Windows のパイプなのに、調べる用意をしていない"
        assert sizes == [], "届いていないのに、読みに行った（待つ読み方を選んだ）"
        os.write(w, b"\n")
        assert got.wait(5), "送った Enter を読まない"
        assert sizes == [1], "届いた分だけを読んでいない"
    finally:
        stop_reading.set()
        thread.join(timeout=5)
        stopped = not thread.is_alive()  # パイプを閉じる前に見る（閉じると、入力の終わりでも終わる）
        os.close(w)
        os.close(r)
    assert stopped, "止める合図で、読むスレッドが終わらない"


def test_on_a_windows_terminal_the_product_keeps_the_waiting_reader(monkeypatch):
    """Windows の端末（コンソール）には、待たない読み方を用意しない（待つ読み方のまま。8.3）。

    製品の Windows の分かれ目を真にし、入力の種類を "tty" と渡す（入力そのものはパイプで代える。見るのは、
    読み方の選び方だけ）。調べる関数を作らず、届いていなくても読みに行って、待つ。"""
    made: list[int] = []
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "windows_pipe_ready", made.append)
    r, w = os.pipe()
    sizes = watch_read_sizes(monkeypatch, r)
    got = threading.Event()
    stop_reading = threading.Event()
    try:
        thread = cli.watch_enter(got.set, "tty", fd=r, stop=stop_reading)
        deadline = time.monotonic() + 5
        while not sizes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sizes == [1024], "端末で、待つ読み方（届いていなくても読みに行って、待つ）になっていない"
        assert made == [], "端末に、待たない読み方を用意した"
        os.write(w, b"\n")
        assert got.wait(5), "送った Enter を読まない"
    finally:
        stop_reading.set()
        os.close(w)  # 読み取りの待ちを切る（Windows の端末の読み方は、0 バイトなら 0.2 秒おいて読み直す）
        thread.join(timeout=5)
        os.close(r)
    assert not thread.is_alive(), "止める合図で、読むスレッドが終わらない"


@pytest.mark.parametrize("error", [
    OSError(errno.EBADF, "Bad file descriptor"),
    AttributeError("module 'ctypes' has no attribute 'WinDLL'"),
    ImportError("No module named 'msvcrt'"),
    ValueError("I/O operation on closed file"),
], ids=["os-error", "attribute-error", "import-error", "value-error"])
def test_when_the_check_cannot_be_prepared_the_product_falls_back_to_the_waiting_reader(monkeypatch, error):
    """調べる用意ができないとき（windows_pipe_ready が失敗する）は、待つ読み方に戻る。起動は失敗しない。

    Enter は読める（8.3 の止まり方は残る）。失敗の種類は、製品が受ける4つ: OS の失敗、API や部品が無い
    （ctypes.WinDLL、msvcrt）、値の誤り（閉じた入力など）。"""
    def cannot_prepare(fd: int):
        raise error

    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "windows_pipe_ready", cannot_prepare)
    r, w = os.pipe()
    sizes = watch_read_sizes(monkeypatch, r)
    got = threading.Event()
    try:
        thread = cli.watch_enter(got.set, "pipe", fd=r)  # 用意の失敗を、外へ出さない
    except BaseException:
        os.close(w)
        os.close(r)
        raise
    try:
        deadline = time.monotonic() + 5
        while not sizes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sizes == [1024], "待つ読み方（届いていなくても読みに行って、待つ）に戻っていない"
        os.write(w, b"\n")
        assert got.wait(5), "待つ読み方に戻った後、Enter を読まない"
    finally:
        os.close(w)  # 入力の終わり
        thread.join(timeout=5)
        os.close(r)
    assert not thread.is_alive(), "書く側が閉じても、読むスレッドが終わらない"


@pytest.mark.parametrize("ending", ["returns", "ctrl-c", "fails"])
def test_main_tells_the_enter_reader_to_stop_when_serving_ends(tmp_path, monkeypatch, capsys, ending):
    """main は、待ち受けが終わったら、Enter を読むスレッドに止める合図を出す。

    ふつうに終わっても、Ctrl+C（KeyboardInterrupt）でも、例外で落ちても。待ち受けている間は、出さない。
    待たない読み方のスレッドは、この合図で眠りから抜けて終わる（合図が無いと、プロセスが終わるまで調べ
    続ける）。アプリ本体ごとの起動では、外から見分けられない（スレッドはプロセスと一緒に終わる）ので、
    main をこの中で呼び、待ち受け（GuardedServer.run）と Enter の読み取り（watch_enter）だけを差し替えて、
    main が渡した合図を見る。"""
    seen: dict = {}

    def fake_watch_enter(on_enter, source, fd=0, stop=None, ready=None):
        seen.update(source=source, stop=stop)
        return threading.Thread()

    def fake_run(self, sockets=None) -> None:
        seen["set_while_serving"] = seen["stop"].is_set()
        if ending == "ctrl-c":
            raise KeyboardInterrupt
        if ending == "fails":
            raise RuntimeError("the server failed")

    monkeypatch.setenv("CIRCUIT_SIM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli, "enter_source", lambda: "pipe")
    monkeypatch.setattr(cli, "watch_enter", fake_watch_enter)
    monkeypatch.setattr(cli.GuardedServer, "run", fake_run)
    argv = ["--no-browser", "--port", "0"]
    if ending == "fails":
        with pytest.raises(RuntimeError, match="the server failed"):
            cli.main(argv)
    else:
        assert cli.main(argv) == 0
        assert Texts(terminal_lang())("stopped") in capsys.readouterr().out
    assert seen["source"] == "pipe"
    assert seen["set_while_serving"] is False, "待ち受けている間に、止める合図を出した"
    assert seen["stop"].is_set(), "待ち受けが終わっても、Enter を読むスレッドに止める合図を出していない"


class PeekNamedPipeStandIn:
    """kernel32.PeekNamedPipe の身代わり。本物の ctypes の関数（C から呼び返される関数）として作る。

    - 受ける側（answer）は、Microsoft の文書の型で引数を受ける C の関数（ctypes.CFUNCTYPE）:
      BOOL PeekNamedPipe(HANDLE, LPVOID, DWORD, LPDWORD, LPDWORD, LPDWORD)
    - 製品に渡すもの（function）は、その番地から作った、型の決まっていない関数。DLL から取り出した関数と
      同じ状態（引数の型は無し、戻り値は int）。製品が決めた argtypes・restype で、引数が C の型に直される。
      決めなければ、Python の整数は C の int として渡り、32 ビットに収まらないハンドルは切り詰められる
    - ctypes.WinDLL と ctypes.get_last_error の身代わりも持つ。ctypes が失敗の理由を控えるのは、
      use_last_error=True で読み込んだ DLL の関数を呼んだときだけ（CPython の文書 ctypes.get_last_error）。
      そうでなければ、控えは 0 のまま

    答えは Microsoft の文書の約束: 成功は 0 でない値で、届いている量を 5 番目の引数の先に書く。失敗は 0 で、
    理由は GetLastError（ここでは error）。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.calls: list[tuple] = []  # C の側に届いた引数（ハンドル、バッファ、大きさ、残りは NULL でないか）
        self.available: int | None = 0  # None なら失敗
        self.error = 0
        self.loaded: list[tuple[str, bool]] = []  # WinDLL の呼ばれ方（名前、use_last_error）
        prototype = ctypes.CFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                     wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD)
        self.callee = prototype(self.answer)  # 持ち続ける（捨てると、番地が無効になる）
        untyped = ctypes.CFUNCTYPE(ctypes.c_int)
        self.function = untyped(ctypes.cast(self.callee, ctypes.c_void_p).value)

    def answer(self, handle, buffer, size, read, available, left) -> int:
        self.calls.append((handle, buffer, size, bool(read), bool(available), bool(left)))
        if self.available is None or not available:
            return 0
        available[0] = self.available
        return 1

    def win_dll(self, name, mode=0, handle=None, use_errno=False, use_last_error=False, winmode=None):
        """ctypes.WinDLL の身代わり（引数の並びは、CPython の ctypes.CDLL と同じ）。"""
        self.loaded.append((str(name), bool(use_last_error)))
        return types.SimpleNamespace(PeekNamedPipe=self.function)

    def get_last_error(self) -> int:
        return self.error if self.loaded and all(kept for _, kept in self.loaded) else 0


def test_windows_pipe_ready_calls_peek_named_pipe_as_documented(monkeypatch):
    """windows_pipe_ready が、PeekNamedPipe を文書どおりに呼び、答えを約束どおりに読む。

    Windows の API を差し替えて流す（どの OS でも）。身代わりは本物の ctypes の関数なので、製品が決めた
    引数の型（argtypes）での変換も、実際に通る（PeekNamedPipeStandIn の説明）。
    差し替える値の出どころ（Microsoft の文書）:
    - PeekNamedPipe(hNamedPipe, lpBuffer, nBufferSize, lpBytesRead, lpTotalBytesAvail,
      lpBytesLeftThisMessage): バッファは NULL でよい（読まない）。成功は 0 でない値、失敗は 0 で、
      理由は GetLastError
    - System Error Codes: ERROR_BROKEN_PIPE 109（The pipe has been ended.）、ERROR_PIPE_NOT_CONNECTED 233
      （No process is on the other end of the pipe.）、ERROR_INVALID_FUNCTION 1、ERROR_INVALID_HANDLE 6
    確かめること:
    - kernel32 を、use_last_error=True で読み込む（そうしないと、失敗の理由が ctypes の控えに入らず、
      書く側が閉じたこと（109、233）を見分けられない）
    - ハンドルは、切り詰めずに渡す（64 ビットの Windows のハンドルは、ポインタの大きさ。ここでは、
      32 ビットに収まらない値で確かめる）。ほかは、量を受け取る所だけを渡す（バッファは NULL、大きさは 0）
    本物の呼び出しは、Windows の試験（パイプを使う上の試験と、下の「子が始まれる」試験）で流れる。"""
    import ctypes
    from ctypes import wintypes

    peek = PeekNamedPipeStandIn()
    big = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8 - 2)) + 0x9A00  # 64 ビットなら、32 ビットに収まらない
    monkeypatch.setattr(ctypes, "WinDLL", peek.win_dll, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", peek.get_last_error, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: big + fd))
    ready = cli.windows_pipe_ready(7)
    loaded = [(name.lower().removesuffix(".dll"), kept) for name, kept in peek.loaded]
    assert loaded == [("kernel32", True)], ("kernel32 を use_last_error=True で読み込んでいない", peek.loaded)
    assert list(peek.function.argtypes) == [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                            wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
    assert peek.function.restype is wintypes.BOOL
    assert ready() == 0
    peek.available = 5
    assert ready() == 5
    assert peek.calls[-1] == (big + 7, None, 0, False, True, False), (
        "ハンドルは切り詰めずに渡す。ほかは、量を受け取る所だけを渡す", peek.calls[-1])
    peek.available = None
    for ended in (109, 233):
        peek.error = ended
        assert ready() is None, f"エラー {ended} は、書く側が閉じた（入力の終わり）"
    for other in (1, 6):
        peek.error = other
        with pytest.raises(OSError, match=f"Windows error {other}"):
            ready()
    assert cli.PIPE_ENDED == {109, 233} == {cli.ERROR_BROKEN_PIPE, cli.ERROR_PIPE_NOT_CONNECTED}


@pytest.mark.skipif(not WINDOWS, reason="Windows の本物の PeekNamedPipe（POのPCの local_ci で流れる）")
def test_windows_pipe_ready_on_a_real_pipe():
    """本物のパイプで: 届いていなければ 0、届いた量、読んだ後は 0、書く側が閉じたら None。"""
    r, w = os.pipe()
    try:
        ready = cli.windows_pipe_ready(r)
        assert ready() == 0
        os.write(w, b"abc")
        assert ready() == 3
        assert os.read(r, 2) == b"ab"
        assert ready() == 1
        assert os.read(r, 1) == b"c"
        assert ready() == 0
        os.close(w)
        w = None
        assert ready() is None
    finally:
        if w is not None:
            os.close(w)
        os.close(r)


# 「標準入力（パイプ）の Enter を待っている間に、標準入力を差し替えずに起動した子が、始まれるか」を確かめる
# 入口（PO の PC で確かめた形と同じ。docs/design/app-design.md の 8.3）。最初の引数で、待ち方を選ぶ:
#   product    製品の読み方（cli.watch_enter）で待つ
#   blocking   読んで待つ（os.read が戻るまで待つ。直す前の読み方）
# 子（python -c pass）が 10 秒のうちに終われば started、終わらなければ HANG を出す。その後、標準入力が閉じる
# まで待つ（Enter を受けたら enter を出す）
CHILD_WHILE_WAITING_ENTRY = """\
import os, subprocess, sys, threading, time

from circuit_sim.app import cli

got = threading.Event()
if sys.argv[1] == "product":
    cli.watch_enter(got.set, "pipe")
else:
    def blocking():
        while os.read(0, 1024):
            got.set()

    threading.Thread(target=blocking, daemon=True).start()
time.sleep(1.0)     # 待ちに入るまで
child = subprocess.Popen([sys.executable, "-c", "pass"])    # 標準入力を差し替えない（webbrowser と同じ）
try:
    child.wait(timeout=10)
    print("started", flush=True)
except subprocess.TimeoutExpired:
    print("HANG", flush=True)
    child.kill()
    child.wait(timeout=10)
print("enter" if got.wait(20) else "no-enter", flush=True)
"""


def child_while_waiting(tmp_path: Path, mode: str) -> tuple[str, str]:
    """（子が始まれたか started / HANG、その後に送った Enter を受けたか enter / no-enter）。"""
    proc = procs.Launched([sys.executable, "-c", CHILD_WHILE_WAITING_ENTRY, mode], cwd=ROOT,
                          env=app_env(tmp_path), stdin=subprocess.PIPE)
    try:
        child = proc.next_line(40).strip()
        proc.stdin.write("\n")
        proc.stdin.flush()
        enter = proc.next_line(40).strip()
        return child, enter
    finally:
        proc.stop()


def test_a_child_can_start_while_the_product_waits_for_enter_on_a_pipe(tmp_path):
    """製品の読み方で標準入力（パイプ）の Enter を待っている間も、標準入力を受け継ぐ子は始まれる。

    Windows では、読んで待っていると子が始まれない（下の対照）。製品の待たない読み方なら、始まれるはず。
    Enter も、その後に読める。Windows でない OS では、どちらの読み方でも始まれる。"""
    assert child_while_waiting(tmp_path, "product") == ("started", "enter")


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の、読んで待つ間は子が始まれないことの対照（POのPCで流れる）")
def test_a_blocking_read_on_a_pipe_keeps_a_child_from_starting_on_windows(tmp_path):
    """対照: 読んで待つ読み方（直す前）だと、Windows では、標準入力を受け継ぐ子が始まれない。

    PO の PC で確かめた形（スレッドが os.read で待っている間に起動した python -c pass は、5 秒たっても
    終わらない）。上の試験が、読み方の違いを見分けられることの確かめ。この対照が成り立たない環境
    （Python か Windows の作りが変わった）では、スキップにして、そう記録する。"""
    child, _enter = child_while_waiting(tmp_path, "blocking")
    if child != "HANG":
        pytest.skip("読んで待つ読み方でも、子が始まれた（この環境では、対照が成り立たない）")


# 端末（疑似端末）を標準入力にして起動する入口。起動されたプロセスは、新しいまとまりの先頭にいる
# （tests/app_procs.py の spawn）。疑似端末を、そのまとまりの端末にしてから、アプリ本体に入れ替わる
TTY_ENTRY = """\
import fcntl, os, sys, termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.execv(sys.executable, [sys.executable, "-m", "circuit_sim.app", *sys.argv[1:]])
"""

only_with_a_pty = pytest.mark.skipif(
    WINDOWS, reason="疑似端末は POSIX だけ（Windows のコンソールからの Enter と Ctrl+C は、手で確かめる）",
)


def launch_on_a_tty(config: Path, *args: str) -> tuple[procs.Launched, int]:
    """アプリ本体を、疑似端末を標準入力にして起動する。（起動したもの、端末の人の側の fd）。"""
    master, slave = os.openpty()
    try:
        proc = procs.Launched([sys.executable, "-c", TTY_ENTRY, "--no-browser", *args], cwd=ROOT,
                              env=app_env(config), stdin=slave)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


@only_with_a_pty
def test_enter_on_a_terminal_gives_a_new_url(tmp_path):
    """端末（tty）から Enter を読む道: 端末で Enter を押すと、新しい URL が出る。

    疑似端末を標準入力にして起動する（人が端末で起動した形。enter_source は "tty"）。"""
    proc, master = launch_on_a_tty(tmp_path)
    try:
        url, lines = read_lines_until_url(proc)
        lines += next_lines(proc, 3)
        assert any("ここで Enter を押すと新しい URL を出します" in ln for ln in lines), lines
        wait_up(url.split("?")[0], START_LIMIT_S)
        os.write(master, b"\n")
        line = proc.wait_line("新しい URL", timeout=5)
        assert URL_RE.search(line).group(0) != url
        os.write(master, b"abc\n")  # 文字を打ってから Enter を押しても同じ
        assert URL_RE.search(proc.wait_line("新しい URL", timeout=5)).group(0) != url
    finally:
        stop(proc)
        os.close(master)


@only_with_a_pty
def test_ctrl_c_on_a_terminal_stops_and_enter_still_works_after_a_warning(tmp_path):
    """端末で Ctrl+C を押して止める道（端末が、前にいるプロセスへ SIGINT を送る）。

    保存していない変更があれば、1回目は知らせて止めない。その後も Enter は効く。2回目で止まり、
    「止めました」を出して、終了コード 0 で終わる。

    知らせは 2 行の文。Enter は、2 行目（と、その終わりの改行）が出てから送る。製品は、文と改行を別々に
    書く（print）。出力をためない設定（PYTHONUNBUFFERED=1、-u）では、1 行目を見てすぐ Enter を送ると、
    知らせの終わりの改行より先に、Enter を読むスレッドの新しい URL が出て、知らせの 2 行目の後ろに1行で
    続くことがある（「新しい URL」で始まる行にならない。40 回に 3〜5 回。検証 6回目の問題1）。"""
    proc, master = launch_on_a_tty(tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
        client.json("api/session", {"doc": doc, "dirty": True}, "PUT")
        os.write(master, b"\x03")  # Ctrl+C
        assert "mine.json" in proc.wait_line("保存していない変更があります", timeout=5)
        proc.wait_line("保存せずに止めるには", contains="もう一度 Ctrl+C を押してください", timeout=5)
        os.write(master, b"\n")
        assert URL_RE.search(proc.wait_line("新しい URL", timeout=5)).group(0) != url
        os.write(master, b"\x03")
        code, out = proc.end(15)
        assert code == 0 and out.rstrip().endswith("circuit-sim を止めました。"), (code, out)
    finally:
        stop(proc)
        os.close(master)


def test_launch_pages_are_removed_on_expiry_shutdown_and_next_start(tmp_path):
    """期限が切れた番号の HTML は消える。止めたときも消える。落ちて残ったものは、次の起動で消える

    （ほかに動いている circuit-sim の新しいものは消さない）。"""
    pages = cli.LaunchPages(tmp_path)
    launches = Launches(seconds=0.1, on_spent=pages.spent)
    expired = pages.write(launches.issue(), "http://a")
    kept = pages.write(launches.issue(), "http://b")
    time.sleep(0.15)
    launches.sweep()
    assert not expired.exists() and not kept.exists()
    left = pages.write("n", "http://c")
    pages.remove_all()
    assert not left.exists()
    old, fresh = tmp_path / "open-old.html", tmp_path / "open-fresh.html"
    old.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    cli.LaunchPages.remove_stale(tmp_path)
    assert not old.exists() and fresh.exists()


@pytest.mark.parametrize(("lang", "note", "renew", "new", "stale"), [
    ("ja_JP.UTF-8", "この URL は1回だけ、5 分以内に使えます", "ここで Enter を押すと新しい URL を出します",
     "新しい URL（1回だけ、5 分以内に使えます）: ", "使い終わったか期限が切れた URL で、画面が開かれました"),
    ("en_US.UTF-8", "This URL works once, within 5 minutes", "press Enter here for a new URL",
     "New URL (works once, within 5 minutes): ", "opened from a URL that was already used or has expired"),
], ids=["ja", "en"])
def test_enter_in_the_terminal_gives_a_new_url(tmp_path, lang, note, renew, new, stale):
    """端末で Enter を押すと、新しい起動の URL を出す。

    使い終わった URL で開かれたら、端末で出し方を案内する。"""
    proc = launch("--no-browser", config=tmp_path, lang=lang, stdin=subprocess.PIPE)
    try:
        url, lines = read_lines_until_url(proc)
        lines += next_lines(proc, 3)
        assert any(note in ln for ln in lines) and any(renew in ln for ln in lines), lines
        wait_ready(Client(url), START_LIMIT_S)
        # 使い終わった URL を、ブラウザでページとして開く（Sec-Fetch-Mode: navigate、Dest: document）
        req = urllib.request.Request(url, headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                                                   "Sec-Fetch-Site": "none"})
        urllib.request.urlopen(req, timeout=5).close()
        line = proc.next_line(5)
        assert stale in line, line
        proc.stdin.write("\n")
        proc.stdin.flush()
        line = proc.next_line(5)  # Enter を読まなくなる後退が入ると、ここで落ちる（待ち続けない）
        assert line.startswith(new), line
        fresh = URL_RE.search(line).group(0)
        assert fresh != url and fresh.split("?")[0] == url.split("?")[0]
        client = Client(fresh)
        client.login()
        assert client.json("api/health")["instance"]
    finally:
        stop(proc)


def test_enter_source_tells_inputs_apart(tmp_path):
    """Enter を読める入力（パイプ、端末）と、読めない入力（/dev/null、Windows の NUL、ファイル）を見分ける。

    Windows の os.isatty は NUL でも True を返すので、GetConsoleMode で見分ける（Windows でも流れる）。"""
    r, w = os.pipe()
    try:
        assert cli.enter_source(r) == "pipe"
    finally:
        os.close(r)
        os.close(w)
    with open(os.devnull, "rb") as null:
        assert cli.enter_source(null.fileno()) is None
    path = tmp_path / "input.txt"
    path.write_text("x", encoding="utf-8")
    with open(path, "rb") as f:
        assert cli.enter_source(f.fileno()) is None


@only_with_a_pty
def test_enter_source_reads_a_terminal_only_in_the_foreground():
    """端末（tty）は、前にいる（その端末のまとまりの先頭の組にいる）ときだけ読む。

    この試験のプロセスは、疑似端末を自分の端末にしていない（前にいない）ので、読まない。前にいるときの道は、
    test_enter_on_a_terminal_gives_a_new_url が、起動したアプリ本体で流す。"""
    master, slave = os.openpty()
    try:
        assert os.isatty(slave) and cli.enter_source(slave) is None
    finally:
        os.close(master)
        os.close(slave)


def test_without_a_readable_input_restart_is_suggested(tmp_path):
    """Enter を読めない起動（入力が /dev/null など）では、起動し直す方法を案内する。"""
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, lines = read_lines_until_url(proc)
        lines += next_lines(proc, 3)
        assert any("circuit-sim を起動し直してください" in ln for ln in lines), lines
        assert not any("Enter" in ln for ln in lines), lines
    finally:
        stop(proc)


@pytest.mark.skipif(WINDOWS, reason="Windows では、別のプロセスに Ctrl+C を送れないため（手で確かめる）")
def test_ctrl_c_stops_right_away_without_unsaved_changes(tmp_path):
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, _ = read_lines_until_url(proc)
        wait_ready(Client(url), START_LIMIT_S)
        proc.send_signal(signal.SIGINT)
        code, out = proc.end(10)
        assert code == 0 and "circuit-sim を止めました" in out, (code, out)
    finally:
        stop(proc)


@pytest.mark.skipif(WINDOWS, reason="Windows では、別のプロセスに Ctrl+C を送れないため（手で確かめる）")
@pytest.mark.parametrize(("lang", "warning", "stopped"), [
    ("ja_JP.UTF-8", "保存していない変更があります", "circuit-sim を止めました"),
    ("en_US.UTF-8", "There are unsaved changes", "circuit-sim has stopped"),
], ids=["ja", "en"])
def test_ctrl_c_with_unsaved_changes_warns_first_then_stops(tmp_path, lang, warning, stopped):
    """未決11: 保存していない変更があれば、1回目の Ctrl+C では止めずに知らせ、2回目で止める。"""
    proc = launch("--no-browser", config=tmp_path, lang=lang)
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
        client.json("api/session", {"doc": doc, "dirty": True}, "PUT")
        proc.send_signal(signal.SIGINT)
        assert "mine.json" in proc.wait_line(warning, timeout=5)
        time.sleep(0.3)
        assert proc.poll() is None, "1回目の Ctrl+C で止まった"
        assert client.json("api/health")["instance"]
        proc.send_signal(signal.SIGINT)
        code, out = proc.end(10)
        assert code == 0 and stopped in out, (code, out)
    finally:
        stop(proc)


def test_unwritable_config_folder_is_reported(tmp_path):
    """設定の場所に書けないときは、理由を出して終わる（終了コード 2）。"""
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x", encoding="utf-8")
    code, out = run_to_end("--no-browser", config=blocker / "sub", lang="en_US.UTF-8")
    assert code == 2
    assert "Can't write to the settings folder" in out


def break_warns_first_then_stops(proc: procs.Launched, send_break) -> None:
    """未決11 の2回押しを、Ctrl+Break（に当たる合図）で確かめる。

    1回目は知らせて止めない。10 秒以内の2回目で止まり、「止めました」を出して、終了コード 0 で終わる。
    どこで落ちても（起動の URL を待つ間、受け付けを待つ間、状態を書く間でも）、起動したものは止める。"""
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
        client.json("api/session", {"doc": doc, "dirty": True}, "PUT")
        send_break()
        assert "mine.json" in proc.wait_line("保存していない変更があります", timeout=5)
        time.sleep(0.3)
        assert proc.poll() is None, "1回目で止まった"
        assert client.json("api/health")["instance"]
        send_break()
        code, out = proc.end(10)
    finally:
        stop(proc)
    assert code == 0 and out.rstrip().endswith("circuit-sim を止めました。"), (code, out)


@pytest.mark.skipif(not WINDOWS, reason="Windows の Ctrl+Break（POのPCの local_ci で流れる）")
def test_ctrl_break_on_windows_warns_first_then_stops(tmp_path):
    """Windows: 別のプロセスに Ctrl+C は送れないが、Ctrl+Break（SIGBREAK）は送れる。

    未決11 の2回押し（1回目は知らせて止めない、10 秒以内の2回目で止める）を確かめる。止めた後は、Ctrl+C と
    同じに「止めました」を出して、終了コード 0 で終わる（PO の PC では、直す前は終了コード 3 で、
    「止めました」も出なかった。直した後の Windows での結果は、まだ無い。cli.break_as_interrupt の説明）。

    Ctrl+C の2回押しと、窓を閉じたときは、手で確かめる（報告の手順）。"""
    proc = launch("--no-browser", config=tmp_path, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    def send_break() -> None:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")

    break_warns_first_then_stops(proc, send_break)


# Windows の Ctrl+Break の経路を、Windows でない OS で流すための入口。製品のコードは変えず、起動する前に
# 3つだけ差し替える:
# - signal.SIGBREAK（Windows にしか無い名前）に、身代わりの合図 SIGUSR1 を当てる
# - uvicorn が受ける合図に、身代わりを足す（uvicorn は、win32 でだけ SIGBREAK を受ける。uvicorn/server.py の
#   HANDLED_SIGNALS）
# - cli.WINDOWS を真にする（製品の Windows の分かれ目）
# 身代わりにできる理由: uvicorn は、止め終えた後に、受けた合図の扱いを元に戻して出し直す
# （Server.capture_signals）。元の扱いが「既定」のままだと、出し直された合図の既定の動きでプロセスが終わり、
# main の終わり（「止めました」、終了コード 0）まで進まない。この流れは OS によらない。違うのは既定の動きの
# 中身だけ: Windows の SIGBREAK は、終了コード 3 で終わる（PO の PC で確かめた）。POSIX の SIGUSR1 は、
# 合図で終わる（終了コードは -10 など）。
BREAK_STAND_IN_ENTRY = """\
import signal, sys

import uvicorn.server

from circuit_sim.app import cli

signal.SIGBREAK = signal.SIGUSR1
uvicorn.server.HANDLED_SIGNALS += (signal.SIGUSR1,)
cli.WINDOWS = True
if sys.argv[1:2] == ["--without-the-fix"]:
    import contextlib

    del sys.argv[1]
    cli.break_as_interrupt = lambda windows: contextlib.nullcontext()
raise SystemExit(cli.main(sys.argv[1:]))
"""


def launch_with_break_stand_in(config: Path, *args: str) -> procs.Launched:
    return procs.Launched([sys.executable, "-c", BREAK_STAND_IN_ENTRY, *args, "--no-browser"], cwd=ROOT,
                          env=app_env(config))


only_with_stand_in = pytest.mark.skipif(
    WINDOWS or not hasattr(signal, "SIGUSR1"),
    reason="Windows では本物の Ctrl+Break の試験が流れる（身代わりの合図 SIGUSR1 は POSIX だけ）",
)


@only_with_stand_in
def test_break_path_warns_first_then_stops_with_exit_code_0(tmp_path):
    """Windows の Ctrl+Break の経路（身代わりの合図で、Windows でない OS でも流す）。

    1回目は知らせて止めない。2回目で止まり、「止めました」を出して、終了コード 0 で終わる。"""
    proc = launch_with_break_stand_in(tmp_path)
    break_warns_first_then_stops(proc, lambda: proc.send_signal(signal.SIGUSR1))


@only_with_stand_in
def test_break_path_without_the_handling_ends_by_the_default_action(tmp_path):
    """対照: 製品の扱い（cli.break_as_interrupt）が無いと、止め終えた後に合図の既定の動きで終わる。

    「止めました」は出ず、終了コードは 0 にならない（PO の PC の Windows では 3。ここでは合図 SIGUSR1 で
    終わる）。上の試験が、扱いの有無を見分けられることの確かめ。"""
    proc = launch_with_break_stand_in(tmp_path, "--without-the-fix")
    try:
        url, _ = read_lines_until_url(proc)
        wait_ready(Client(url), START_LIMIT_S)
        proc.send_signal(signal.SIGUSR1)
        code, out = proc.end(10)
        assert code == -signal.SIGUSR1 and "circuit-sim を止めました" not in out, (code, out)
    finally:
        stop(proc)


def test_break_as_interrupt_only_touches_the_break_signal_on_windows_and_puts_it_back(monkeypatch):
    """break_as_interrupt の中では、Windows の SIGBREAK が KeyboardInterrupt を出す。出たら、元の扱いに戻る。

    Windows でなければ何もしない。SIGBREAK の無い OS でも落ちない。主スレッドの外からは何もしない
    （主スレッドの外で合図の扱いを決めようとすると、Python は ValueError を出す。スレッドが例外なく終わり、
    中で見た扱いが元のままだったことを確かめる）。"""
    stand_in = getattr(signal, "SIGBREAK", None) or getattr(signal, "SIGUSR2", None)
    if stand_in is None:
        pytest.skip("身代わりにできる合図が無い")
    before = signal.getsignal(stand_in)

    def inside(windows: bool) -> object:
        with cli.break_as_interrupt(windows):
            return signal.getsignal(stand_in)

    def from_another_thread(windows: bool) -> list:
        """別のスレッドで呼んだ結果: [中で見た扱い] か、[出た例外]。"""
        result: list = []

        def run() -> None:
            try:
                result.append(inside(windows))
            except BaseException as exc:  # スレッドの中の例外を、試験まで運ぶ
                result.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        return result

    try:
        if not hasattr(signal, "SIGBREAK"):
            assert inside(True) is before  # SIGBREAK の無い OS: Windows の分かれ目でも、何も変えず、落ちない
            monkeypatch.setattr(signal, "SIGBREAK", stand_in, raising=False)
        # ここからは、SIGBREAK がある（本物か、身代わり）
        assert inside(False) is before, "Windows でない OS で、合図の扱いを変えた"
        assert from_another_thread(True) == [before], "主スレッドの外で、扱いを変えた（または例外で落ちた）"
        assert from_another_thread(False) == [before]
        assert inside(True) is signal.default_int_handler, "Windows の主スレッドで、扱いを決めていない"
        assert signal.getsignal(stand_in) is before, "出た後に、元の扱いに戻っていない"
        with pytest.raises(KeyboardInterrupt):
            with cli.break_as_interrupt(True):
                assert signal.getsignal(stand_in) is signal.default_int_handler
                signal.raise_signal(stand_in)
                time.sleep(1)  # 合図は、次の命令の切れ目で届く
        assert signal.getsignal(stand_in) is before, "例外で抜けた後に、元の扱いに戻っていない"
    finally:
        signal.signal(stand_in, before)


@pytest.mark.skipif(not WINDOWS,
                    reason="Windows の待ち受けの結び方と Ctrl+Break（POのPCの local_ci で流れる）")
def test_restart_on_windows_binds_the_same_port_while_a_browser_keeps_connections(tmp_path):
    """Windows: 待っている接続（ブラウザの keep-alive）を開いたまま止めて、すぐ同じポートで起動し直せる。

    Windows の待ち受けには、どの設定も付けない（cli.bind の説明。SO_EXCLUSIVEADDRUSE を付けると、受け付けた
    接続が残っている間は同じポートに結べない）。止めるときは、どの接続もふつうに閉じる（RST で捨てない）。
    Ctrl+Break で止めたときの終了コードは 0。"""
    import http.client

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def start() -> procs.Launched:
        return launch("--no-browser", "--port", str(port), config=tmp_path,
                      creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    proc = start()
    conns = []
    try:
        url, _ = read_lines_until_url(proc)
        client = Client(url)
        wait_ready(client, START_LIMIT_S)
        cookie = "; ".join(f"{c.name}={c.value}" for c in client.jar)
        for _ in range(3):  # 応答を読み終えて、つないだままにする
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/health", headers={"Cookie": cookie})
            conn.getresponse().read()
            conns.append(conn)
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")
        assert proc.wait(timeout=15) == 0
        proc = start()
        url2, lines = read_lines_until_url(proc)
        assert f":{port}/" in url2, ("同じポートで起動し直せなかった", lines)
        assert client.json("api/health")["instance"]  # 前のクッキーのまま戻れる
    finally:
        for conn in conns:
            conn.close()
        stop(proc)


def test_app_test_ids_stay_short(request):
    """アプリの試験（tests/test_app_*.py）の ID は 200 文字まで。parametrize の長い値には、短い ids を付ける。

    pytest は、実行中の試験の ID を環境変数 PYTEST_CURRENT_TEST に入れる。Windows の環境変数は 32767 文字
    までで、超えると試験の準備の段階でエラーになる（POのPCで、値が数万文字の ID の試験が 4 件）。日本語の
    値は、ID の中で1文字が 6 文字（\\uXXXX）になる。集めた試験だけを見るので、一部だけ流したときは、
    その分だけ確かめる。"""
    too_long = [f"{len(item.nodeid)} 文字: {item.nodeid[:100]}…" for item in request.session.items
                if item.nodeid.startswith("tests/test_app_") and len(item.nodeid) > 200]
    assert not too_long, too_long
