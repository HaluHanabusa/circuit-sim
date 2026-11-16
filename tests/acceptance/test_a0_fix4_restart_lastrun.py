"""A0 の独立検証（verifier、4回目）: 起動し直したとき前回のポートへ戻る仕組み（D-061）と、前回の起動の記録
（last-run.json）が壊れている・古い・別のプロセスが同じ番号を持つときの動き。ブラウザは要らない。

記録は docs/verification/A0-verifier-4.md。POの Windows の PC でも動く（Windows の結び方は、その OS の実物で
確かめることになる。Linux で Windows の結び方を強制しても、Linux の TIME_WAIT の決まりで結べないので、
OS の違いを強制では確かめられない）。

- 止めた直後に、同じポートへ結び直せる: 待ちの接続（keep-alive）を残したまま止め、相手が閉じない場合
  （こちらは FIN_WAIT_2）と、相手が閉じた場合（こちらは TIME_WAIT）。アプリ本体と同じ部品（GuardedServer、
  cli.bind）で、同じプロセスの中で動かす（どの OS でも止め方が同じになる）。
- 前回のポートを試す順と、待つかどうかの決め方（bind_any）。前回のプロセスが無く、そのポートで待ち受けて
  いるものも無いときだけ、決めた時間まで結び直す。ほかのプログラムが待ち受けていれば待たない（居座らない）。
  前回のプロセスが動いていれば待たない。--port を決めたら、記録は見ない。
- 記録の読み取り: 壊れた形はどれも「記録なし」。前回のプロセス ID が大きすぎるとき、起動が例外で落ちない
  （軽微の不具合。xfail）。記録の名前が名前付きパイプ（FIFO）のとき、起動が止まらない（軽微の不具合。xfail。
  POSIX だけ）。
- 端末の文言: ファイルを渡して起動すると、オプションではなくファイルだと分かる文（UX 確認3回目の改善4）。

ポートは 28470 番台を使わない（決めた空きのポートを、前回のポートとして渡す）。既定の並び（28470 から）は、
結ぶ関数を差し替えて、試した順だけを見る。
時間の基準: 「待たない」は 1 秒未満（待たない道は、結ぶ1回と接続の確かめ1回。Windows の接続の確かめは 0.3 秒で
打ち切る）。「待つ」は、渡した待ち時間（1 秒）以上、その 3 倍未満（混んだ機械のため）。
製品の不具合で落ちる試験は xfail(strict=True)。直ったら XPASS で落ちるので、印を外す。
"""

from __future__ import annotations

import errno
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"
POSIX = os.name == "posix"
TOKEN = "v" * 43
WAIT = 30.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def dead_pid() -> int:
    """終わったプロセスの ID（すぐには使い回されない）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def in_use(port: int) -> OSError:
    exc = OSError(errno.EADDRINUSE, "in use (test)")
    return exc


def only(port: int, calls: list[int] | None = None):
    """port だけ本物の cli.bind で結び、ほかのポートは「使用中」にする（28470 番台に触らない）。"""
    from circuit_sim.app import cli

    def binder(p: int) -> socket.socket:
        if calls is not None:
            calls.append(p)
        if p == port:
            return cli.bind(p)
        raise in_use(p)

    return binder


class Other:
    """ほかのプログラムの代わり。listen=False なら、結ぶだけで待ち受けない。"""

    def __init__(self, port: int, listen: bool = True) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", port))
        if listen:
            self.sock.listen(8)

    def close(self) -> None:
        self.sock.close()


# ------------------------------------------------------------------ 止めた直後に、同じポートへ結び直せる


def serve(tmp_path: Path, sock: socket.socket):
    import uvicorn

    from circuit_sim.app import cli
    from circuit_sim.app.config import Settings
    from circuit_sim.app.server import Launches, Workspace, create_app

    workspace = Workspace()
    app = create_app(token=TOKEN, settings=Settings(tmp_path / "config"), workspace=workspace,
                     launches=Launches())
    server = cli.GuardedServer(uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off",
                                              timeout_graceful_shutdown=2),
                               workspace, say=lambda text: None)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + WAIT
    while not server.started:
        assert time.monotonic() < deadline, "サーバーが起動しない"
        time.sleep(0.02)
    return server, thread


def keep_alive(port: int) -> socket.socket:
    """画面の代わり: 合言葉つきの要求を1つ送り、応答を読み切って、接続は開いたままにする。"""
    conn = socket.create_connection(("127.0.0.1", port), timeout=10)
    conn.sendall(f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                 f"Cookie: circuit_sim_key={TOKEN}\r\n\r\n".encode())
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(65536)
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head
    length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n")
                      if line.lower().startswith(b"content-length:")))
    while len(body) < length:
        body += conn.recv(65536)
    return conn


@pytest.mark.parametrize("client", ["keeps-open", "closes"])
def test_rebind_the_same_port_right_after_stopping_with_idle_connections(tmp_path, client):
    """待ちの接続を3本残して止め、前回の記録を渡して結び直すと、同じポートに戻る（待つ時間の上限の中で）。"""
    from circuit_sim.app import cli

    port = free_port()
    sock = cli.bind(port)
    server, thread = serve(tmp_path, sock)
    conns = [keep_alive(port) for _ in range(3)]
    try:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()
        sock.close()
        for conn in conns:  # こちらの FIN が届いたか（相手から見て、読み終わり）
            conn.settimeout(5)
            assert conn.recv(1) == b""
        if client == "closes":
            for conn in conns:
                conn.close()  # こちらは TIME_WAIT へ
            time.sleep(0.2)
        t0 = time.monotonic()
        again, failures = cli.bind_any(None, binder=only(port), previous=(port, dead_pid()))
        elapsed = time.monotonic() - t0
        try:
            assert again is not None and again.getsockname()[1] == port, failures
            assert elapsed < cli.RESTART_WAIT_SECONDS, elapsed
        finally:
            if again is not None:
                again.close()
    finally:
        for conn in conns:
            conn.close()


# ------------------------------------------------------------------ 前回のポートを試す順と、待つかどうか


def test_previous_port_is_tried_first_then_the_default_order():
    from circuit_sim.app import cli

    calls: list[int] = []
    sock, failures = cli.bind_any(None, binder=only(-1, calls), previous=(41234, dead_pid()),
                                  listening=lambda p: True)
    assert sock is None
    assert calls == [41234, cli.DEFAULT_PORT, *cli.FALLBACK_PORTS, 0], calls
    calls.clear()
    cli.bind_any(None, binder=only(-1, calls), previous=(cli.DEFAULT_PORT, dead_pid()),
                 listening=lambda p: True)
    assert calls == [cli.DEFAULT_PORT, *cli.FALLBACK_PORTS, 0], calls  # 同じポートを2回試さない
    calls.clear()
    cli.bind_any(None, binder=only(-1, calls), previous=None)
    assert calls == [cli.DEFAULT_PORT, *cli.FALLBACK_PORTS, 0], calls


def test_port_option_ignores_the_previous_run():
    from circuit_sim.app import cli

    calls: list[int] = []
    wanted = free_port()
    sock, failures = cli.bind_any(wanted, binder=only(wanted, calls), previous=(free_port(), dead_pid()))
    try:
        assert sock is not None and sock.getsockname()[1] == wanted
        assert calls == [wanted]
    finally:
        if sock is not None:
            sock.close()


def test_no_wait_when_another_program_listens_on_the_previous_port():
    """前回のプロセスは無いが、ほかのプログラムが待ち受けている: 待たずに次へ（居座らない）。"""
    from circuit_sim.app import cli

    port = free_port()
    other = Other(port, listen=True)
    calls: list[int] = []
    try:
        t0 = time.monotonic()
        sock, failures = cli.bind_any(None, binder=only(port, calls), previous=(port, dead_pid()), wait=5.0)
        elapsed = time.monotonic() - t0
        assert sock is None
        assert calls.count(port) == 1, calls
        assert elapsed < 1.0, elapsed
        assert failures[0][0] == port
    finally:
        other.close()


def test_no_wait_when_the_previous_process_is_alive():
    """記録のプロセス ID が動いている（前回のアプリ本体がまだ動いている、または ID が使い回された）:

    待たない。"""
    from circuit_sim.app import cli

    port = free_port()
    other = Other(port, listen=False)
    calls: list[int] = []
    try:
        t0 = time.monotonic()
        sock, _ = cli.bind_any(None, binder=only(port, calls), previous=(port, os.getpid()), wait=5.0)
        assert sock is None
        assert calls.count(port) == 1, calls
        assert time.monotonic() - t0 < 1.0
    finally:
        other.close()


def test_waits_at_most_the_given_time_when_nothing_listens():
    """前回のプロセスは無く、待ち受けているものも無いのに結べない: 決めた時間まで結び直し、それから次へ。"""
    from circuit_sim.app import cli

    port = free_port()
    other = Other(port, listen=False)
    try:
        t0 = time.monotonic()
        sock, failures = cli.bind_any(None, binder=only(port), previous=(port, dead_pid()), wait=1.0)
        elapsed = time.monotonic() - t0
        assert sock is None
        assert 1.0 <= elapsed < 3.0, elapsed
        assert failures[0][0] == port
    finally:
        other.close()


def test_returns_to_the_previous_port_once_it_is_released():
    """結び直しの間に前回の後始末が終われば（ここでは 0.6 秒後に放す）、前回のポートで起動する。"""
    from circuit_sim.app import cli

    port = free_port()
    other = Other(port, listen=False)
    timer = threading.Timer(0.6, other.close)
    timer.start()
    try:
        t0 = time.monotonic()
        sock, failures = cli.bind_any(None, binder=only(port), previous=(port, dead_pid()), wait=5.0)
        elapsed = time.monotonic() - t0
        try:
            assert sock is not None and sock.getsockname()[1] == port, failures
            assert 0.5 <= elapsed < 3.0, elapsed
        finally:
            if sock is not None:
                sock.close()
    finally:
        timer.cancel()
        other.close()


# ------------------------------------------------------------------ 記録の読み取り

BROKEN = [
    ("空", b""),
    ("JSON でない", b"{port: 1"),
    ("null", b"null"),
    ("並び", b"[41000, 1]"),
    ("文字列", b'"41000"'),
    ("キーが無い", b'{"port": 41000}'),
    ("port が文字列", b'{"port": "41000", "pid": 1}'),
    ("port が小数", b'{"port": 41000.0, "pid": 1}'),
    ("port が真偽", b'{"port": true, "pid": 1}'),
    ("port が 0", b'{"port": 0, "pid": 1}'),
    ("port が負", b'{"port": -41000, "pid": 1}'),
    ("port が 65536", b'{"port": 65536, "pid": 1}'),
    ("port がとても長い数", b'{"port": 1' + b"0" * 5000 + b', "pid": 1}'),
    ("pid が 0", b'{"port": 41000, "pid": 0}'),
    ("pid が負", b'{"port": 41000, "pid": -1}'),
    ("pid が文字列", b'{"port": 41000, "pid": "x"}'),
    ("pid が null", b'{"port": 41000, "pid": null}'),
    ("UTF-8 でない", b"\xff\xfe\x00{"),
    ("深い入れ子", b"[" * 200000),
    ("NaN", b'{"port": NaN, "pid": 1}'),
    ("NUL", b"\x00" * 4096),
]


@pytest.mark.parametrize(("name", "raw"), BROKEN, ids=[b[0] for b in BROKEN])
def test_broken_record_reads_as_none(tmp_path, name, raw):
    from circuit_sim.app.config import read_last_run

    (tmp_path / "last-run.json").write_bytes(raw)
    assert read_last_run(tmp_path) is None


def test_record_round_trip_and_private(tmp_path):
    from circuit_sim.app.config import read_last_run, write_last_run

    write_last_run(tmp_path, 41000, 4242)
    assert read_last_run(tmp_path) == (41000, 4242)
    saved = json.loads((tmp_path / "last-run.json").read_text(encoding="utf-8"))
    assert saved == {"port": 41000, "pid": 4242}
    if POSIX:
        assert (tmp_path / "last-run.json").stat().st_mode & 0o777 == 0o600


def test_record_that_is_a_folder_reads_as_none(tmp_path):
    from circuit_sim.app.config import read_last_run

    (tmp_path / "last-run.json").mkdir()
    assert read_last_run(tmp_path) is None


@pytest.mark.xfail(
    strict=not WINDOWS,
    reason=(
        "製品の不具合(軽微): 記録の pid が C の long に入らない（2^31 以上）と、POSIX の pid_alive の "
        "os.kill が "
        "OverflowError を出し、前回のポートが使用中なら起動が例外（トレースバック、終了コード 1）で落ちる。"
        "記録はアプリ本体が書くので、壊れたときか書き換えたときだけ。"
        "Windows は ctypes が切り詰めるので落ちない"
    ),
)
@pytest.mark.parametrize("pid", [2 ** 31, 10 ** 30])
def test_huge_pid_in_the_record_does_not_crash(tmp_path, pid):
    """main と同じ形（bind_any(…, previous=read_last_run(場所))）で、前回のポートが

    ほかのプログラムに使われている。"""
    from circuit_sim.app import cli
    from circuit_sim.app.config import read_last_run

    port = free_port()
    (tmp_path / "last-run.json").write_text(json.dumps({"port": port, "pid": pid}), encoding="utf-8")
    other = Other(port, listen=True)
    try:
        sock, failures = cli.bind_any(None, binder=only(port), previous=read_last_run(tmp_path), wait=1.0)
        assert sock is None and failures[0][0] == port
    finally:
        other.close()


def read_lines(proc: subprocess.Popen, q: queue.Queue) -> None:
    for raw in proc.stdout:
        q.put(raw.decode("utf-8", "replace"))
    q.put(None)


@pytest.mark.skipif(not POSIX, reason="名前付きパイプ（mkfifo）は POSIX だけ")
@pytest.mark.xfail(
    strict=True,
    reason=(
        "製品の不具合(軽微): 記録の名前が名前付きパイプだと、read_last_run の read_text が書き手を待ち続け、"
        "起動が止まる（URL も誤りも出ない。--port を付けても同じ）。設定ファイル（settings.json）は同じ形でも"
        "起動する。そのような物を置けるのは本人だけ"
    ),
)
def test_fifo_record_does_not_hang_the_start(tmp_path):
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    fifo = config / "last-run.json"
    os.mkfifo(fifo)
    env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(config), "LANG": "ja_JP.UTF-8",
           "PYTHONIOENCODING": "utf-8"}
    argv = [sys.executable, "-m", "circuit_sim.app", "--no-browser", "--port", str(free_port())]
    proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    q: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=read_lines, args=(proc, q), daemon=True).start()
    lines: list[str] = []
    try:
        deadline = time.monotonic() + 15
        started = False
        while time.monotonic() < deadline and not started:
            try:
                line = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            lines.append(line)
            started = "http://127.0.0.1:" in line
        assert started, ("15 秒たっても URL が出ない", "".join(lines), proc.poll())
    finally:
        try:  # 止まっていたら、書き手を開いて閉じ、読み取りを終わらせる
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        proc.kill()
        proc.wait(timeout=20)


# ------------------------------------------------------------------ 端末の文言（UX 確認3回目の改善4）


@pytest.mark.parametrize(("lang", "expected"), [
    ("ja_JP.UTF-8", "circuit-sim は、起動するときにファイルを受け取りません（examples/rlc_series.json）。"),
    ("en_US.UTF-8", "circuit-sim doesn't take a file when it starts (examples/rlc_series.json)."),
], ids=["ja", "en"])  # 文を ID にしない（日本語は1文字が 6 文字になり、294 文字の ID になっていた）
def test_file_argument_is_told_as_a_file_not_an_option(tmp_path, lang, expected):
    env = {**os.environ, "CIRCUIT_SIM_CONFIG_DIR": str(tmp_path / "config"), "LANG": lang, "LC_ALL": lang,
           "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, "-m", "circuit_sim.app", "examples/rlc_series.json"], cwd=ROOT,
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 2, r
    assert expected in r.stderr, r.stderr
    assert "知らないオプション" not in r.stderr and "Unknown option" not in r.stderr, r.stderr
    # 対照: - で始まるものは、いまのまま知らないオプション
    r = subprocess.run([sys.executable, "-m", "circuit_sim.app", "--bogus"], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and ("知らないオプション" in r.stderr or "Unknown option" in r.stderr), r.stderr
