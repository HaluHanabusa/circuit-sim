"""起動コマンド circuit-sim（circuit_sim/app/cli.py）の試験。別のプロセスで起動して確かめる。

設定の場所は、試験ごとの一時フォルダ（CIRCUIT_SIM_CONFIG_DIR）。端末の文言の言語は LC_ALL で決める。
起動したときの URL（/?k=…）に載るのは、一度だけ使える起動の番号（合言葉ではない）。試験も画面と同じく、
その URL を1回開いてクッキーを受け取る。2つ目の URL が要るときは、端末に Enter を送って出させる。

ブラウザを開く起動（--no-browser を付けない起動）は、必ず launch_with_fake_browser で起動する。偽のブラウザが
失敗しても、OS の既定のブラウザ（本物）へ進ませない見張りを付けて起動する（BROWSER_GUARD の説明）。

Windows だけの分岐を、OS の呼び出しを差し替えて Linux でも流す試験は、差し替える値を実機の値に合わせる。
出どころは、それぞれの試験に書く（CPython 3.13 のソース、Microsoft の文書、PO の PC での結果）。
"""
from __future__ import annotations

import errno
import http.cookiejar
import json
import os
import queue
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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


def launch(*args: str, config: Path, lang: str = "ja_JP.UTF-8", env: dict | None = None,
           stdin=subprocess.DEVNULL) -> subprocess.Popen:
    full_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(config),
                "LC_ALL": lang, **(env or {})}
    return subprocess.Popen(
        [sys.executable, "-m", "circuit_sim.app", *args],
        cwd=ROOT,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=stdin,
        text=True,
        encoding="utf-8",
    )


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


def browser_command(script: Path) -> str:
    """BROWSER に渡す、偽のブラウザのコマンド（%s に開く場所が入る）。

    webbrowser は、%s を含む指定を shlex（POSIX の決まり）で分けるので、\\ は / に直し（Windows の API は
    どちらも受ける）、空白を含む場所は引用符で包む。"""
    return " ".join(shlex.quote(part.replace("\\", "/")) for part in (sys.executable, str(script))) + " %s"


def fake_browser_env(script: Path) -> dict[str, str]:
    """偽のブラウザ script を BROWSER に置く環境変数。偽が失敗した記録は script と同じ場所の .failed。"""
    return {"BROWSER": browser_command(script),
            "CIRCUIT_SIM_TEST_BROWSER_FAILED": str(script.with_suffix(".failed"))}


def launch_with_fake_browser(script: Path, *, config: Path, lang: str = "ja_JP.UTF-8",
                             stdin=subprocess.DEVNULL) -> subprocess.Popen:
    """ブラウザを開く起動（--no-browser 無し）。ブラウザは偽の script だけ（見張りつき。BROWSER_GUARD）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(config), "LC_ALL": lang,
           **fake_browser_env(script)}
    env.pop("CIRCUIT_SIM_TEST_BROWSER_OPEN", None)
    return subprocess.Popen(
        [sys.executable, "-c", BROWSER_GUARD],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=stdin,
        text=True,
        encoding="utf-8",
    )


def fake_browser_failure(script: Path) -> str | None:
    """偽のブラウザが失敗していたら、そのとき渡された場所。失敗していなければ None。"""
    failed = script.with_suffix(".failed")
    return failed.read_text(encoding="utf-8") if failed.exists() else None


def read_lines_until_url(proc: subprocess.Popen, timeout: float = 15) -> tuple[str, list[str]]:
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        lines.append(line)
        m = URL_RE.search(line)
        if m:
            return m.group(0), lines
        if proc.poll() is not None and not line:
            break
    raise AssertionError(f"起動した場所の表示が無い: {lines}")


def pump_lines(proc: subprocess.Popen) -> tuple[queue.Queue, threading.Thread]:
    """端末の出力の残りを、別のスレッドで読み続ける（行が出ないときに、試験が待ち続けないように）。

    始めた後は、proc.stdout をほかから読まない（止めるときは kill と wait。communicate は使わない）。"""
    lines: queue.Queue = queue.Queue()

    def loop() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        except ValueError:
            pass  # 読んでいる間に閉じられた
        lines.put(None)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return lines, thread


def wait_for_line(lines: queue.Queue, start: str, timeout: float) -> str:
    """start で始まる行が出るまで待つ。timeout 秒のうちに出なければ、それまでの行を添えて落とす。"""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        try:
            line = lines.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            raise AssertionError(f"{start!r} で始まる行が {timeout} 秒たっても出ない: {seen}") from None
        if line is None:
            raise AssertionError(f"{start!r} で始まる行が出ないまま、アプリ本体が終わった: {seen}")
        seen.append(line)
        if line.startswith(start):
            return line


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


def stop(proc: subprocess.Popen) -> str:
    if proc.poll() is None:
        proc.kill()
    out, _ = proc.communicate(timeout=10)
    return out


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
        proc = launch("--no-browser", "--port", str(port), config=tmp_path, lang=lang)
        proc.wait(timeout=20)
        out = proc.communicate()[0]
    assert proc.returncode == 2
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
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=20)
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
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path),
           "LC_ALL": "ja_JP.UTF-8"}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0

    def start(*args: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "circuit_sim.app", "--no-browser", *args], cwd=ROOT,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=flags)

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
    """Windows のソケットのエラー code（WSAGetLastError の値）を、Python が Windows で作るのと同じ形にする。

    Python は、ソケットの失敗を OSError(errno, 文, None, winerror) にする（Modules/socketmodule.c の
    set_error → PyErr_SetExcFromWindowsErr）。errno は PC/errmap.h の winerror_to_errno が決める:
    - WSAEACCES（10013）は 10000 を引いた 13（EACCES）。例外の型は PermissionError
    - WSAEADDRINUSE（10048）などは、そのまま 10048（Windows の errno.EADDRINUSE も 10048。
      Modules/errnomodule.c が、errno.h の値ではなく WSA の値を使う）
    winerror の項目は、Windows でない OS の Python では作られないので、後から足す。"""
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


def test_windows_errors_in_the_tests_have_the_shape_python_gives_them():
    """差し替えに使う Windows のエラーが、実機の形（errno と winerror の組）になっている。"""
    denied, in_use = winerror(10013), winerror(10048)
    assert (denied.errno, denied.winerror) == (13, 10013) and isinstance(denied, PermissionError)
    assert (in_use.errno, in_use.winerror) == (10048, 10048)


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
    proc = launch("--no-browser", "--port", "1", config=tmp_path)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2
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

    netsh に出る範囲のすべてが拒まれるわけではない。* の印の範囲（管理者が足した除外）は、動的なポートの
    割り当てから外すだけで、決めて結ぶことはできる（PO の PC では、50000 が「使用中」と案内された）。
    だから、拒まれるかどうかは、製品と同じ結び方で実際に結んで確かめてから選ぶ。"""
    probed = windows_reserved_ports()
    shown = "、".join(shown_bind_result(*item) for item in probed) or "予約の範囲が無い"
    record_property("reserved_ports", shown)
    refused = [port for port, _, exc in probed if exc is not None and getattr(exc, "winerror", None) == 10013]
    if not refused:
        pytest.skip(f"この PC には、OS に拒まれる（10013）予約のポートが無い: {shown}")
    port = refused[0]
    proc = launch("--no-browser", "--port", str(port), config=tmp_path)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2
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
        proc = launch("--no-browser", "--port", str(port), config=tmp_path / str(port))
        out = proc.communicate(timeout=20)[0]
        assert proc.returncode == 2, (shown, out)
        assert expected in out, (shown, out)
        code = getattr(exc, "winerror", None)
        assert (code == 10048) == ("ほかのプログラムが使っています" in out), (shown, out)
        assert (code == 10013) == ("使えません" in out and "netsh" in out), (shown, out)


@pytest.mark.parametrize(("lang", "words"), [
    ("ja_JP.UTF-8",
     ["使い方:", "オプション", "待ち受けるポート", "ブラウザを開かない", "この説明を出して終わる"]),
    ("en_US.UTF-8", ["usage:", "options", "port to listen on", "don't open a browser", "show this help"]),
])
def test_help_is_in_one_language(tmp_path, lang, words):
    proc = launch("--help", config=tmp_path, lang=lang)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 0
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

    （UX の改善5）"""
    proc = launch(*args, config=tmp_path, lang=lang)
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2 and text in out, out
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
    done = subprocess.run([sys.executable, "-m", "circuit_sim.app", "--help"], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    assert ("使い方:" in done.stdout) == (expected == "ja"), (hex(langid), done.stdout)
    assert ("usage:" in done.stdout) == (expected == "en"), (hex(langid), done.stdout)


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


def wait_for_browser_record(record: Path, script: Path, proc: subprocess.Popen, timeout: float) -> list[str]:
    """偽のブラウザが控えた引数。呼ばれなければ、偽のブラウザが失敗していたかどうかを添えて落とす。"""
    deadline = time.monotonic() + timeout
    while not record.exists():
        if time.monotonic() > deadline:
            failed = fake_browser_failure(script)
            raise AssertionError(
                "偽のブラウザが呼ばれない（ブラウザを開く呼び出しが戻っていない）" if failed is None
                else f"偽のブラウザが失敗した（渡された場所: {failed.strip()}）"
            )
        assert proc.poll() is None, "アプリ本体が終わった"
        time.sleep(0.05)
    return json.loads(record.read_text(encoding="utf-8"))


@pytest.mark.parametrize("stdin", [subprocess.DEVNULL, subprocess.PIPE], ids=["stdin-null", "stdin-pipe"])
def test_browser_gets_only_the_location_of_a_private_page(tmp_path, stdin):
    """既定のブラウザ（BROWSER の指定で、偽のブラウザに差し替える）に渡すのは、転送用の HTML の場所だけ。

    ブラウザの起動の引数（ほかの利用者にも /proc/<pid>/cmdline で見える）には、起動の番号も合言葉も出ない。
    HTML は本人だけが読める（POSIX は 0600、場所は 0700。Windows の権限は test_app_config.py）。中身は端末に
    出した URL へ移るだけ。番号が使われたら消える。

    stdin-pipe は、アプリ本体が標準入力のパイプから Enter を読む起動（ほかのプログラムから起動された形）。
    Windows では、アプリ本体がパイプを読んで待っている間に起動されたブラウザのコマンドは、始まれずに止まる
    （POのPCで、受け入れ試験の偽のブラウザが 30 秒たっても呼ばれなかった）。読み始めるのを、ブラウザを開く
    呼び出しが戻った後にしたので、どちらの入力でも START_LIMIT_S のうちに呼ばれる。Windows でも流れる。"""
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
        lines += [proc.stdout.readline() for _ in range(3)]
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


@pytest.mark.parametrize("outcome", ["opened", "not-opened", "raised"])
def test_open_when_ready_tells_when_the_browser_call_has_returned(monkeypatch, outcome):
    """after_open は、ブラウザを開く呼び出しが戻った後に1回だけ呼ばれる（開けても、開けなくても、例外でも）。

    main は、ここで端末の Enter を読み始める。呼び出しの最中には呼ばれない。"""
    events: list[str] = []

    def fake_open(target):
        events.append("open-start")
        if outcome == "raised":
            raise RuntimeError("no display")
        events.append("open-end")
        return outcome == "opened"

    monkeypatch.setattr(cli.webbrowser, "open", fake_open)
    said: list[str] = []
    cli.open_when_ready(FakeServer(), "file:///x.html", "http://u", said.append, Texts("en"), lambda: True,
                        wait=0.3, after_open=lambda: events.append("after"))
    assert events[0] == "open-start" and events[-1] == "after" and events.count("after") == 1
    assert bool(said) == (outcome != "opened")


def test_open_when_ready_does_not_start_reading_when_the_app_is_already_stopping(monkeypatch):
    class Stopping:
        started = False
        should_exit = True

    monkeypatch.setattr(cli.webbrowser, "open", lambda target: pytest.fail("止めている最中に開いた"))
    called: list[str] = []
    cli.open_when_ready(Stopping(), "file:///x.html", "http://u", called.append, Texts("en"),
                        after_open=lambda: called.append("after"))
    assert called == []


def test_watch_enter_can_be_created_first_and_started_later():
    """Enter を読むスレッドは、作るだけにして、後から始められる（start=False）。始めるまでは読まない。"""
    r, w = os.pipe()
    got = threading.Event()
    try:
        thread = cli.watch_enter(got.set, "pipe", fd=r, start=False)
        os.write(w, b"\n")
        assert not thread.is_alive() and not got.wait(0.2), "始める前に読んでいる"
        thread.start()
        assert got.wait(5), "始めた後、先に送っておいた Enter を読まない"
    finally:
        os.close(w)  # 入力の終わりで、読むスレッドが終わる
        thread.join(timeout=5)
        os.close(r)
    assert not thread.is_alive()


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


def test_enter_is_read_only_after_the_browser_call_returns(tmp_path):
    """端末の Enter を読み始めるのは、ブラウザを開く呼び出しが戻ってから。

    偽のブラウザを 1 秒待たせ、その間に Enter を送っておく。新しい URL が出るのは、偽のブラウザが終わった後
    （先に読み始めると、Windows では標準入力を共有するブラウザのコマンドが始まれず、どの OS でも、端末で動く
    ブラウザとキー入力を取り合う）。送っておいた Enter は捨てない（後で読んで、新しい URL を出す）。"""
    script, record = recording_browser(tmp_path, before="time.sleep(1.0)")
    proc = launch_with_fake_browser(script, config=tmp_path / "config", stdin=subprocess.PIPE)
    pump = None
    try:
        url, _ = read_lines_until_url(proc)
        proc.stdin.write("\n")
        proc.stdin.flush()
        lines, pump = pump_lines(proc)
        line = wait_for_line(lines, "新しい URL", START_LIMIT_S + 5)
        assert record.exists(), "偽のブラウザが終わる前に、Enter を読んでいる"
        assert URL_RE.search(line).group(0) != url
    finally:
        proc.kill()
        proc.wait(timeout=10)
        if pump is not None:
            pump.join(timeout=10)
    assert fake_browser_failure(script) is None


@pytest.mark.parametrize(("code", "failed"), [(0, False), (1, True)], ids=["fake-ok", "fake-fails"])
def test_fake_browser_guard_never_falls_through_to_the_real_browser(tmp_path, code, failed):
    """試験の見張り（BROWSER_GUARD）: 偽のブラウザが失敗しても、webbrowser.open はそこで終わる。

    見張りが無いと、webbrowser は偽の次に OS の既定のブラウザ（本物）を試す。見張りは、偽の結果がどうでも
    「開いた」と答えて止め、失敗は控えに残す。"""
    script = tmp_path / "fake_browser.py"
    script.write_text(f"import sys\nsys.exit({code})\n", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **fake_browser_env(script),
           "CIRCUIT_SIM_TEST_BROWSER_OPEN": "file:///circuit-sim-test/never-opened.html"}
    done = subprocess.run([sys.executable, "-c", BROWSER_GUARD], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True"  # 次の候補（OS の既定のブラウザ）へ進まない
    if failed:
        assert fake_browser_failure(script) == "file:///circuit-sim-test/never-opened.html\n"
    else:
        assert fake_browser_failure(script) is None


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
        lines += [proc.stdout.readline() for _ in range(3)]
        assert any(note in ln for ln in lines) and any(renew in ln for ln in lines), lines
        wait_ready(Client(url), START_LIMIT_S)
        # 使い終わった URL を、ブラウザでページとして開く（Sec-Fetch-Mode: navigate、Dest: document）
        req = urllib.request.Request(url, headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                                                   "Sec-Fetch-Site": "none"})
        urllib.request.urlopen(req, timeout=5).close()
        line = proc.stdout.readline()
        assert stale in line, line
        proc.stdin.write("\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
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


def test_without_a_readable_input_restart_is_suggested(tmp_path):
    """Enter を読めない起動（入力が /dev/null など）では、起動し直す方法を案内する。"""
    proc = launch("--no-browser", config=tmp_path)
    try:
        url, lines = read_lines_until_url(proc)
        lines += [proc.stdout.readline() for _ in range(3)]
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
        assert proc.wait(timeout=10) == 0
        assert "circuit-sim を止めました" in proc.stdout.read()
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
        line = ""
        deadline = time.monotonic() + 5
        while warning not in line and time.monotonic() < deadline:
            line = proc.stdout.readline()
        assert "mine.json" in line
        time.sleep(0.3)
        assert proc.poll() is None, "1回目の Ctrl+C で止まった"
        assert client.json("api/health")["instance"]
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 0
        assert stopped in proc.stdout.read()
    finally:
        stop(proc)


def test_unwritable_config_folder_is_reported(tmp_path):
    """設定の場所に書けないときは、理由を出して終わる（終了コード 2）。"""
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x", encoding="utf-8")
    proc = launch("--no-browser", config=blocker / "sub", lang="en_US.UTF-8")
    out = proc.communicate(timeout=20)[0]
    assert proc.returncode == 2
    assert "Can't write to the settings folder" in out


def break_warns_first_then_stops(proc: subprocess.Popen, send_break) -> None:
    """未決11 の2回押しを、Ctrl+Break（に当たる合図）で確かめる。

    1回目は知らせて止めない。10 秒以内の2回目で止まり、「止めました」を出して、終了コード 0 で終わる。"""
    url, _ = read_lines_until_url(proc)
    client = Client(url)
    wait_ready(client, START_LIMIT_S)
    doc = {"file": "mine.json", "template": "rlc_series", "text": "{}", "sample": None}
    client.json("api/session", {"doc": doc, "dirty": True}, "PUT")
    send_break()
    lines, pump = pump_lines(proc)
    try:
        assert "mine.json" in wait_for_line(lines, "保存していない変更があります", 5)
        time.sleep(0.3)
        assert proc.poll() is None, "1回目で止まった"
        assert client.json("api/health")["instance"]
        send_break()
        code = proc.wait(timeout=10)
        try:
            stopped = wait_for_line(lines, "circuit-sim を止めました", 5)
        except AssertionError as exc:
            raise AssertionError(f"終了コード {code}。{exc}") from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        pump.join(timeout=10)
    assert code == 0 and stopped.strip() == "circuit-sim を止めました。"


@pytest.mark.skipif(not WINDOWS, reason="Windows の Ctrl+Break（POのPCの local_ci で流れる）")
def test_ctrl_break_on_windows_warns_first_then_stops(tmp_path):
    """Windows: 別のプロセスに Ctrl+C は送れないが、Ctrl+Break（SIGBREAK）は送れる。

    未決11 の2回押し（1回目は知らせて止めない、10 秒以内の2回目で止める）を確かめる。止めた後は、Ctrl+C と
    同じに「止めました」を出して、終了コード 0 で終わる（POのPCでは、直す前は終了コード 3 で、「止めました」も
    出なかった。cli.break_as_interrupt の説明）。

    Ctrl+C の2回押しと、窓を閉じたときは、手で確かめる（報告の手順）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path),
           "LC_ALL": "ja_JP.UTF-8"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "circuit_sim.app", "--no-browser"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )

    def send_break() -> None:
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            proc.kill()
            pytest.skip("この実行には端末（コンソール）が無いので、Ctrl+Break を送れなかった")

    try:
        break_warns_first_then_stops(proc, send_break)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# Windows の Ctrl+Break の経路を、Windows でない OS で流すための入口。製品のコードは変えず、起動する前に
# 3つだけ差し替える:
# - signal.SIGBREAK（Windows にしか無い名前）に、身代わりの合図 SIGUSR1 を当てる
# - uvicorn が受ける合図に、身代わりを足す（uvicorn は、win32 でだけ SIGBREAK を受ける。uvicorn/server.py の
#   HANDLED_SIGNALS）
# - cli.WINDOWS を真にする（製品の Windows の分かれ目）
# 身代わりにできる理由: uvicorn は、止め終えた後に、受けた合図の扱いを元に戻して出し直す
# （Server.capture_signals）。元の扱いが「既定」のままだと、出し直された合図の既定の動きでプロセスが終わり、
# main の終わり（「止めました」、終了コード 0）まで進まない。この流れは OS によらない。違うのは既定の動きの
# 中身だけ: Windows の SIGBREAK は、C ランタイムが終了コード 3 で終える（ucrt の raise。POのPCで 3 だった）。
# POSIX の SIGUSR1 は、合図で終わる（終了コードは -10 など）。
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


def launch_with_break_stand_in(config: Path, *args: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(config),
           "LC_ALL": "ja_JP.UTF-8"}
    return subprocess.Popen(
        [sys.executable, "-c", BREAK_STAND_IN_ENTRY, *args, "--no-browser"], cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
        encoding="utf-8",
    )


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

    「止めました」は出ず、終了コードは 0 にならない（POのPCの Windows では 3。ここでは合図 SIGUSR1 で
    終わる）。上の試験が、扱いの有無を見分けられることの確かめ。"""
    proc = launch_with_break_stand_in(tmp_path, "--without-the-fix")
    try:
        url, _ = read_lines_until_url(proc)
        wait_ready(Client(url), START_LIMIT_S)
        proc.send_signal(signal.SIGUSR1)
        assert proc.wait(timeout=10) == -signal.SIGUSR1
        assert "circuit-sim を止めました" not in proc.stdout.read()
    finally:
        stop(proc)


def test_break_as_interrupt_only_touches_the_break_signal_on_windows_and_puts_it_back(monkeypatch):
    """break_as_interrupt の中では、Windows の SIGBREAK が KeyboardInterrupt を出す。出たら、元の扱いに戻る。

    Windows でなければ何もしない。SIGBREAK の無い OS でも落ちない。主スレッドの外からは何もしない。"""
    stand_in = getattr(signal, "SIGBREAK", None) or getattr(signal, "SIGUSR2", None)
    if stand_in is None:
        pytest.skip("身代わりにできる合図が無い")
    before = signal.getsignal(stand_in)

    def inside(windows: bool, seen: list) -> None:
        with cli.break_as_interrupt(windows):
            seen.append(signal.getsignal(stand_in))

    try:
        seen: list = []
        inside(False, seen)
        if not hasattr(signal, "SIGBREAK"):
            inside(True, seen)  # SIGBREAK の無い OS: 何もしない
            monkeypatch.setattr(signal, "SIGBREAK", stand_in, raising=False)
        outside = threading.Thread(target=inside, args=(True, seen))
        outside.start()
        outside.join(timeout=5)
        assert seen and all(handler is before for handler in seen), "扱いを変えないはずの場面で変えた"
        with pytest.raises(KeyboardInterrupt):
            with cli.break_as_interrupt(True):
                assert signal.getsignal(stand_in) is signal.default_int_handler
                signal.raise_signal(stand_in)
                time.sleep(1)  # 合図は、次の命令の切れ目で届く
        assert signal.getsignal(stand_in) is before, "出た後に、元の扱いに戻っていない"
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

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path),
           "LC_ALL": "ja_JP.UTF-8"}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def start():
        return subprocess.Popen(
            [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(port)], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
            encoding="utf-8", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

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
